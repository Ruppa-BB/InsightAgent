"""Run the main demo against the configured PostgreSQL and print reconciled evidence."""
import json
from uuid import uuid4
from backend.app.agent.provider import parse_demo
from backend.app.agent.service import execute_intent
from backend.app.serialization import json_ready

if __name__ == '__main__':
    question = '为什么2011年2月销售额比1月下降'
    result = execute_intent(parse_demo(question).intent, question, uuid4(), 'demo')
    print(json.dumps(json_ready({key: result[key] for key in ('id','answer','comparison','warnings')}), ensure_ascii=False, indent=2))
    for attribution in result['attributions']:
        print(attribution['dimension'], attribution['group_count'], attribution['delta_sum'], attribution['reconciled'])
