from datetime import date
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.tools import dashboard_tool as dashboard
from backend.app.errors import ServiceError


def test_dashboard_guards(monkeypatch):
    quality={'summary': {'start_date':date(2009,12,1),'last_date':date(2011,12,9)},'blocking':True}
    monkeypatch.setattr(dashboard,'current_quality',lambda c:quality)
    connection=Mock()
    with pytest.raises(ServiceError,match='质量'):
        dashboard.query_dashboard(date(2011,2,1),connection)
    with pytest.raises(ServiceError,match='完整覆盖'):
        dashboard.query_dashboard(date(2011,12,1),connection)
    with pytest.raises(ValueError):
        dashboard.query_dashboard(date(2011,2,2),connection)
    connection.execute.assert_not_called()


def test_dashboard_api_validation(monkeypatch):
    monkeypatch.setattr(dashboard,'query_dashboard',lambda month:{'month':month})
    client=TestClient(app)
    assert client.get('/api/dashboard?month=2011-02-01').json()=={'month':'2011-02-01'}
    assert client.get('/api/dashboard?month=invalid').status_code==422
