"""Proposed original-service entry, not deployed. Default old callback is unchanged."""
import os
import app as engine
from second_line_adapter import install
app = engine.app
required = ['LINE_NEW_YM_SYSTEM', 'LINE_NEW_YM_PASS', 'LINE_NEW_BRIDGE_SECRET']
if all(os.environ.get(key) for key in required):
    install(vars(engine), system=os.environ['LINE_NEW_YM_SYSTEM'],
            password=os.environ['LINE_NEW_YM_PASS'], secret=os.environ['LINE_NEW_BRIDGE_SECRET'])
    engine.log.info('Separate new-line song callback configured')
else:
    engine.log.info('Separate new-line song callback disabled: scoped settings incomplete')
