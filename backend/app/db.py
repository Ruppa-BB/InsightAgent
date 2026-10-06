from contextlib import contextmanager
from collections.abc import Iterator

from sqlalchemy import Connection, create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from backend.app.config import Settings, settings
from backend.app.errors import ServiceError

engine = (
    create_engine(settings.database_url, pool_pre_ping=True, pool_timeout=5,
                  connect_args={'connect_timeout': 5})
    if settings.database_url else None
)


@contextmanager
def read_connection() -> Iterator[Connection]:
    """All tools in one analysis see a single, read-only database snapshot."""
    if engine is None:
        raise ServiceError('database_unconfigured', '请在 .env 配置 DATABASE_URL。', 503)
    try:
        with engine.connect().execution_options(isolation_level='REPEATABLE READ') as connection:
            with connection.begin():
                connection.execute(text('SET TRANSACTION READ ONLY'))
                from backend.app.data_management.versions import source_info
                source=source_info()
                connection.info['dataset_source']=source
                if source['schema']:
                    connection.exec_driver_sql(f"SET LOCAL search_path TO \"{source['schema']}\", pg_catalog")
                connection.execute(
                    text("SELECT set_config('statement_timeout', :timeout, true)"),
                    {'timeout': f'{settings.query_timeout_ms}ms'},
                )
                yield connection
    except SQLAlchemyError as exc:
        state = getattr(getattr(exc, 'orig', None), 'sqlstate', None)
        if state == '57014':
            raise ServiceError('query_timeout', '数据库查询超时，请缩小日期范围后重试。', 504) from exc
        raise ServiceError('database_unavailable', '数据库查询失败，请检查 PostgreSQL 服务、连接配置和表结构。', 503) from exc
