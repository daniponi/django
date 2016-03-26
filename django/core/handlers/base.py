from __future__ import unicode_literals

import logging
import sys
import types
import warnings

from django import http
from django.conf import settings
from django.core import signals
from django.core.exceptions import (
    ImproperlyConfigured, MiddlewareNotUsed, PermissionDenied,
    SuspiciousOperation,
)
from django.db import connections, transaction
from django.http.multipartparser import MultiPartParserError
from django.urls import get_resolver, get_urlconf, set_urlconf
from django.utils import six
from django.utils.deprecation import RemovedInDjango20Warning
from django.utils.encoding import force_text
from django.utils.module_loading import import_string
from django.views import debug

logger = logging.getLogger('django.request')


def get_exception_response(request, status_code, exception):
    resolver = get_resolver(get_urlconf())

    try:
        callback, param_dict = resolver.resolve_error_handler(status_code)
        # Unfortunately, inspect.getargspec result is not trustable enough
        # depending on the callback wrapping in decorators (frequent for handlers).
        # Falling back on try/except:
        try:
            response = callback(request, **dict(param_dict, exception=exception))
        except TypeError:
            warnings.warn(
                "Error handlers should accept an exception parameter. Update "
                "your code as this parameter will be required in Django 2.0",
                RemovedInDjango20Warning, stacklevel=2
            )
            response = callback(request, **param_dict)
    except SystemExit:
        # Allow sys.exit() to actually exit. See tickets #1023 and #4701
        raise

    except Exception:  # Handle everything else.
        # Get the exception info now, in case another exception is thrown later.
        # FIXME: BaseHandler here as sender is not nice, but who cares?
        signals.got_request_exception.send(sender=BaseHandler, request=request)
        return handle_uncaught_exception(request, sys.exc_info())

    return response


def handle_uncaught_exception(request, exc_info):
    resolver = get_resolver(get_urlconf())
    if settings.DEBUG_PROPAGATE_EXCEPTIONS:
        raise

    logger.error('Internal Server Error: %s', request.path,
        exc_info=exc_info,
        extra={
            'status_code': 500,
            'request': request
        }
    )

    if settings.DEBUG:
        return debug.technical_500_response(request, *exc_info)

    # If Http500 handler is not installed, re-raise last exception
    if resolver.urlconf_module is None:
        six.reraise(*exc_info)
    # Return an HttpResponse that displays a friendly error message.
    callback, param_dict = resolver.resolve_error_handler(500)
    return callback(request, **param_dict)


class ExceptionMiddleware(object):
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            response = self.get_response(request)
        except http.Http404 as exc:
            logger.warning('Not Found: %s', request.path,
                        extra={
                            'status_code': 404,
                            'request': request
                        })
            if settings.DEBUG:
                response = debug.technical_404_response(request, exc)
            else:
                response = get_exception_response(request, 404, exc)

        except PermissionDenied as exc:
            logger.warning(
                'Forbidden (Permission denied): %s', request.path,
                extra={
                    'status_code': 403,
                    'request': request
                })
            response = get_exception_response(request, 403, exc)

        except MultiPartParserError as exc:
            logger.warning(
                'Bad request (Unable to parse request body): %s', request.path,
                extra={
                    'status_code': 400,
                    'request': request
                })
            response = get_exception_response(request, 400, exc)

        except SuspiciousOperation as exc:
            # The request logger receives events for any problematic request
            # The security logger receives events for all SuspiciousOperations
            security_logger = logging.getLogger('django.security.%s' %
                            exc.__class__.__name__)
            security_logger.error(
                force_text(exc),
                extra={
                    'status_code': 400,
                    'request': request
                })
            if settings.DEBUG:
                return debug.technical_500_response(request, *sys.exc_info(), status_code=400)

            response = get_exception_response(request, 400, exc)

        except SystemExit:
            # Allow sys.exit() to actually exit. See tickets #1023 and #4701
            raise

        except Exception:  # Handle everything else.
            # Get the exception info now, in case another exception is thrown later.
            # FIXME: BaseHandler here as sender is not nice, but who cares?
            signals.got_request_exception.send(sender=BaseHandler, request=request)
            return handle_uncaught_exception(request, sys.exc_info())

        return response


class BaseHandler(object):
    # Changes that are always applied to a response (in this order).
    response_fixes = [
        http.conditional_content_removal,
    ]

    def __init__(self):
        self._request_middleware = None
        self._view_middleware = None
        self._template_response_middleware = None
        self._response_middleware = None
        self._exception_middleware = None
        self._middleware_chain = None

    def load_middleware(self):
        """
        Populate middleware lists from settings.MIDDLEWARE_CLASSES.

        Must be called after the environment is fixed (see __call__ in subclasses).
        """
        self._request_middleware = []
        self._view_middleware = []
        self._template_response_middleware = []
        self._response_middleware = []
        self._exception_middleware = []

        # settings.MIDDLEWARE = settings.MIDDLEWARE_CLASSES
        if settings.MIDDLEWARE is None:
            handler = self._legacy_get_response
            self._legacy_load_middleware()
        else:
            handler = self._get_response
            for middleware_path in reversed(settings.MIDDLEWARE):
                middleware = import_string(middleware_path)
                try:
                    mw_instance = middleware(handler)
                except MiddlewareNotUsed as exc:
                    if settings.DEBUG:
                        if six.text_type(exc):
                            logger.debug('MiddlewareNotUsed(%r): %s', middleware_path, exc)
                        else:
                            logger.debug('MiddlewareNotUsed: %r', middleware_path)
                    continue

                if mw_instance is None:
                    raise ImproperlyConfigured(
                        'Middleware factory %s returned None.' % middleware_path
                    )

                if hasattr(mw_instance, 'process_view'):
                    self._view_middleware.insert(0, mw_instance.process_view)
                if hasattr(mw_instance, 'process_template_response'):
                    self._template_response_middleware.append(mw_instance.process_template_response)

                handler = mw_instance

        handler = ExceptionMiddleware(handler)

        # We only assign to this when initialization is complete as it is used
        # as a flag for initialization being complete.
        self._middleware_chain = handler

    def make_view_atomic(self, view):
        non_atomic_requests = getattr(view, '_non_atomic_requests', set())
        for db in connections.all():
            if (db.settings_dict['ATOMIC_REQUESTS']
                    and db.alias not in non_atomic_requests):
                view = transaction.atomic(using=db.alias)(view)
        return view

    def get_response(self, request):
        "Returns an HttpResponse object for the given HttpRequest"
        # Setup default url resolver for this thread
        set_urlconf(settings.ROOT_URLCONF)

        response = self._middleware_chain(request)

        try:
            response = self._legacy_apply_response_middleware(request, response)
            response = self.apply_response_fixes(request, response)
        except Exception:  # Any exception should be gathered and handled
            signals.got_request_exception.send(sender=self.__class__, request=request)
            response = handle_uncaught_exception(request, sys.exc_info())

        response._closable_objects.append(request)

        # If the exception handler returns a TemplateResponse that has not
        # been rendered, force it to be rendered.
        if not getattr(response, 'is_rendered', True) and callable(getattr(response, 'render', None)):

            response = response.render()

        return response

    def _get_response(self, request):
        "Returns an HttpResponse object for the given HttpRequest"
        response = None

        if hasattr(request, 'urlconf'):
            urlconf = request.urlconf
            set_urlconf(urlconf)
            resolver = get_resolver(urlconf)
        else:
            resolver = get_resolver()

        resolver_match = resolver.resolve(request.path_info)
        callback, callback_args, callback_kwargs = resolver_match
        request.resolver_match = resolver_match

        # Apply view middleware
        for middleware_method in self._view_middleware:
            response = middleware_method(request, callback, callback_args, callback_kwargs)
            if response:
                return response

        wrapped_callback = self.make_view_atomic(callback)

        try:
            response = wrapped_callback(request, *callback_args, **callback_kwargs)
        except Exception as e:
            response = self._legacy_process_exception_by_middleware(e, request)

        # Complain if the view returned None (a common error).
        if response is None:
            if isinstance(callback, types.FunctionType):    # FBV
                view_name = callback.__name__
            else:                                           # CBV
                view_name = callback.__class__.__name__ + '.__call__'

            raise ValueError("The view %s.%s didn't return an HttpResponse object. It returned None instead."
                             % (callback.__module__, view_name))

        # If the response supports deferred rendering, apply template
        # response middleware and then render the response
        elif hasattr(response, 'render') and callable(response.render):
            for middleware_method in self._template_response_middleware:
                response = middleware_method(request, response)
                # Complain if the template response middleware returned None (a common error).
                if response is None:
                    raise ValueError("%s.process_template_response didn't return an "
                        "HttpResponse object. It returned None instead."
                        % (middleware_method.__self__.__class__.__name__))

            try:
                response = response.render()
            except Exception as e:
                response = self._legacy_process_exception_by_middleware(e, request)

        return response

    def apply_response_fixes(self, request, response):
        """
        Applies each of the functions in self.response_fixes to the request and
        response, modifying the response in the process. Returns the new
        response.
        """
        for func in self.response_fixes:
            response = func(request, response)
        return response

    # LEGACY methods, remove after old style middlewares are removed.
    def _legacy_process_exception_by_middleware(self, exception, request):
        """
        Pass the exception to the exception middleware. If no middleware
        return a response for this exception, raise it.
        """
        for middleware_method in self._exception_middleware:
            response = middleware_method(request, exception)
            if response:
                return response
        raise

    def _legacy_apply_response_middleware(self, request, response):
        # Apply response middleware, regardless of the response
        for middleware_method in self._response_middleware:
            response = middleware_method(request, response)
            # Complain if the response middleware returned None (a common error).
            if response is None:
                raise ValueError(
                    "%s.process_response didn't return an "
                    "HttpResponse object. It returned None instead."
                    % (middleware_method.__self__.__class__.__name__))

        return response

    def _legacy_get_response(self, request):
        response = None
        # Apply request middleware
        for middleware_method in self._request_middleware:
            response = middleware_method(request)
            if response:
                break

        if response is None:
            response = self._get_response(request)
        return response

    def _legacy_load_middleware(self):
        for middleware_path in settings.MIDDLEWARE_CLASSES:
            mw_class = import_string(middleware_path)
            try:
                mw_instance = mw_class()
            except MiddlewareNotUsed as exc:
                if settings.DEBUG:
                    if six.text_type(exc):
                        logger.debug('MiddlewareNotUsed(%r): %s', middleware_path, exc)
                    else:
                        logger.debug('MiddlewareNotUsed: %r', middleware_path)
                continue

            if hasattr(mw_instance, 'process_request'):
                self._request_middleware.append(mw_instance.process_request)
            if hasattr(mw_instance, 'process_view'):
                self._view_middleware.append(mw_instance.process_view)
            if hasattr(mw_instance, 'process_template_response'):
                self._template_response_middleware.insert(0, mw_instance.process_template_response)
            if hasattr(mw_instance, 'process_response'):
                self._response_middleware.insert(0, mw_instance.process_response)
            if hasattr(mw_instance, 'process_exception'):
                self._exception_middleware.insert(0, mw_instance.process_exception)
