# -*- coding: utf-8 -*-
import json, sys, os
_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _here)
import tool_synthesis as _ts
payload = json.loads(sys.stdin.read())
try:
    _ts._run_tests_child(payload['source'], payload['func'],
                         payload['tests'])
    print(json.dumps({'ok': True}))
except BaseException as exc:
    print(json.dumps({'ok': False, 'error':
                      type(exc).__name__ + ': ' + str(exc)[:300]}))
