import os
import logging
import time
import time
import os
import logging
import traceback
from logging.handlers import RotatingFileHandler
from functools import wraps
from flask import g, request, current_app
from flask_login import current_user
from azure.monitor.opentelemetry import configure_azure_monitor
from functools import wraps
from sqlalchemy import event
from sqlalchemy.engine import Engine


class JsonFormatter(logging.Formatter):
    """Custom JSON-style formatter"""
    def format(self, record):
        log_obj = {
            'timestamp': self.formatTime(record),
            'level': record.levelname,
            'module': record.module,
            'function': record.funcName,
            'line': record.lineno,
            'message': record.getMessage()
        }
        if hasattr(record, 'duration_ms'):
            log_obj['duration_ms'] = record.duration_ms
        if hasattr(record, 'user_id'):
            log_obj['user_id'] = record.user_id
        return str(log_obj)

def setup_logging(app):
    """Configure application logging"""
    if os.getenv('FLASK_ENV') != 'development':
        configure_azure_monitor(
            logger_name=__name__,
        )
        gunicorn_logger = logging.getLogger('gunicorn.error')
        app.logger.handlers = gunicorn_logger.handlers
        app.logger.setLevel(logging.INFO)
    
    # Create logs directory
    log_dir = os.path.join(app.root_path, 'logs')
    os.makedirs(log_dir, exist_ok=True)

    # Configure handlers
    handlers = {
        'app.log': (logging.INFO, 'General application logs'),
        'error.log': (logging.ERROR, 'Error logs only'),
        'database.log': (logging.DEBUG, 'Database operations'),
        'access.log': (logging.INFO, 'Request/response logs')
    }

    formatter = JsonFormatter()

    for filename, (level, _) in handlers.items():
        handler = RotatingFileHandler(
            os.path.join(log_dir, filename),
            maxBytes=10*1024*1024,  # 10MB
            backupCount=5
        )
        handler.setFormatter(formatter)
        handler.setLevel(level)
        app.logger.addHandler(handler)

    app.logger.setLevel(logging.INFO)
    return app.logger

def setup_request_logging(app):
    """Configure request logging middleware"""
    @app.before_request
    def log_request_info():
        g.start_time = time.time()
        app.logger.info(
            "Request started",
            extra={
                'method': request.method,
                'path': request.path,
                'ip': request.remote_addr,
                'user_agent': request.headers.get('User-Agent'),
                'user_id': current_user.id if hasattr(current_user, 'id') else None
            }
        )

    @app.after_request
    def log_response_info(response):
        duration_ms = (time.time() - g.start_time) * 1000
        app.logger.info(
            "Request completed",
            extra={
                'duration_ms': duration_ms,
                'status_code': response.status_code,
                'content_length': response.content_length
            }
        )
        return response
    
    # Add error handler
    @app.errorhandler(Exception)
    def handle_exception(e):
        app.logger.error(
            f"""Unhandled exception: {
                str(
                    {
                        'error': str(e),
                        'traceback': traceback.format_exc(),
                        'path': request.path,
                        'method': request.method,
                        'user_id': current_user.id if current_user.is_authenticated else 'User Not Logged In'
                    }
                )
            }"""
        )
        return "Internal Server Error", 500

def log_performance(category):
    """Performance monitoring decorator"""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            from flask import current_app
            start_time = time.time()
            try:
                result = f(*args, **kwargs)
                duration_ms = (time.time() - start_time) * 1000
                current_app.logger.info(
                    f"{category} operation completed",
                    extra={
                        'duration_ms': duration_ms,
                        'operation': f.__name__,
                        'user_id': current_user.id if hasattr(current_user, 'id') else None
                    }
                )
                return result
            except Exception as e:
                current_app.logger.error(
                    f"{category} operation failed",
                    extra={
                        'operation': f.__name__,
                        'error': str(e)
                    }
                )
                raise
        return wrapper
    return decorator

def log_db_operation(operation_type):
    """Decorator for logging database operations"""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            start_time = time.time()
            try:
                result = f(*args, **kwargs)
                duration_ms = (time.time() - start_time) * 1000
                current_app.logger.info(
                    f"""Database {operation_type}: {
                        str(
                            {
                                'operation': f.__name__,
                                'args': args,
                                'kwargs': kwargs,
                                'duration_ms': round(duration_ms, 2)
                            }
                        )   
                    }""",
                )
                return result
            except Exception as e:
                duration_ms = (time.time() - start_time) * 1000
                current_app.logger.error(
                    f"""Database {operation_type} failed: {
                        str(
                            {
                                'operation': f.__name__,
                                'duration_ms': round(duration_ms, 2),
                                'error': str(e),
                                'success': False
                            }
                        )
                    }"""
                )
                raise
        return wrapper
    return decorator

def setup_db_logging(app, db):
    """Configure database operation logging"""
    
    # Log all SQL statements
    @event.listens_for(Engine, "before_cursor_execute")
    def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        conn.info.setdefault('query_start_time', []).append(time.time())
        app.logger.debug(
            f"""SQL Query Started: {
                str(
                    {
                        'statement': statement,
                        'parameters': parameters,
                        'executemany': executemany
                    }
                )
            }"""
        )

    @event.listens_for(Engine, "after_cursor_execute")
    def after_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        total = time.time() - conn.info['query_start_time'].pop(-1)
        current_app.logger.debug(
            f"""SQL Query Completed: {
                str(
                    {
                        'duration_ms': round(total * 1000, 2),
                        'statement': statement,
                        'parameters': parameters,
                        'executemany': executemany
                    }
                )
            }"""
        )

    # Log connection pool events
    @event.listens_for(db.engine, "checkout")
    def receive_checkout(dbapi_connection, connection_record, connection_proxy):
        current_app.logger.debug(
            f"""Database connection checked out: {
                str(
                    {
                        'pool_id': id(connection_record),
                        'connection_id': id(dbapi_connection)
                    }
                )
            }"""
        )

    @event.listens_for(db.engine, "checkin")
    def receive_checkin(dbapi_connection, connection_record):
        current_app.logger.debug(
            f"""Database connection checked in: {
                str(
                    {
                        'pool_id': id(connection_record),
                        'connection_id': id(dbapi_connection)
                    }
                )
            }"""
        )