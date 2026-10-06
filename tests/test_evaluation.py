from copy import deepcopy
from decimal import Decimal
from unittest.mock import Mock

import pytest

from backend.app.agent.schemas import ParsedQuestion
from backend.app.config import settings
from scripts.evaluate_agent import compare_parsed, load_cases, result_errors, run_suite, summary


def test_suite_has_unique_valid_gold_and_coverage():
    suite = load_cases()
    assert 20 <= len(suite['cases']) <= 30
    assert {'english','empty','zero_baseline','unsafe_request','truncation','followup','unsupported_filter'} <= {c['category'] for c in suite['cases']}


def test_parser_comparison_catches_lost_filter_and_wrong_dates():
    expected = load_cases()['cases'][9]['expected']
    actual = ParsedQuestion.model_validate(expected)
    assert compare_parsed(actual,expected) == []
    actual.intent.filters.country = None
    assert any('filters' in e for e in compare_parsed(actual,expected))
    actual = ParsedQuestion(status='needs_clarification',message='请给年份')
    assert compare_parsed(actual,expected)


def test_report_denominator_keeps_errors_and_excludes_not_applicable():
    result = summary([{'status':s} for s in ('passed','failed','error','not_applicable')])
    assert result['evaluated'] == 3
    assert result['pass_rate'] == 1/3
    assert summary([{'status':'not_applicable'}])['pass_rate'] is None


def test_numeric_oracle_catches_wrong_amount_and_false_zero_ratio():
    actual = {'comparison':{'previous':Decimal('0'),'current':Decimal('20'),'delta':Decimal('20'),'change_ratio':None},
              'attributions':[],'warnings':['基期为 0'],'answer':'','trace':[]}
    oracle = {'comparison':{'previous':'0','current':'20','delta':'20','change_ratio':None}}
    assert result_errors(actual,oracle,{'change_ratio_null':True}) == []
    altered = deepcopy(actual)
    altered['comparison']['current'] = Decimal('21')
    assert result_errors(altered,oracle,{})
    altered = deepcopy(actual)
    altered['comparison']['change_ratio'] = 0
    assert result_errors(altered,oracle,{'change_ratio_null':True})


def test_truncation_and_warning_checks_do_not_accept_short_results():
    actual = {'data':[{'dimension':'x','value':Decimal('1')}], 'trace':[{'result':{'truncated':True}}],
              'warnings':[], 'answer':''}
    oracle = {'rows':[{'dimension':'x','value':'1.00'}],'truncated':True}
    assert result_errors(actual,oracle,{}) == []
    assert result_errors(actual,oracle,{'warning_contains':['截断']})
    actual['trace'] = []
    assert result_errors(actual,oracle,{})


def test_live_budget_and_unknown_ids_block_before_database(tmp_path):
    suite = load_cases()
    with pytest.raises(ValueError,match='1–5'):
        run_suite(suite,'live',[],tmp_path/'report.json')
    with pytest.raises(ValueError,match='unknown IDs'):
        run_suite(suite,'offline',['Q99'],tmp_path/'report.json')


def test_offline_never_uses_remote_provider_and_restores_store(tmp_path,monkeypatch):
    from scripts import evaluate_agent as runner
    remote = Mock(side_effect=AssertionError('remote must not run'))
    monkeypatch.setattr(runner.provider,'parse_question',remote)
    monkeypatch.setattr(runner,'data_signature',lambda:{'summary':'fixture'})
    old_store = settings.analysis_store
    report = run_suite(load_cases(),'offline',['Q20'],tmp_path/'report.json')
    remote.assert_not_called()
    assert report['parsing_summary']['passed'] == 1
    assert report['execution_summary']['evaluated'] == 0
    assert settings.analysis_store == old_store
