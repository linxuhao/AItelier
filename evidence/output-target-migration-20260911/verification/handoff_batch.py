import json,subprocess,sys,os
from pathlib import Path
import skillflow
assert '/site-packages/' in skillflow.__file__, skillflow.__file__
print('INSTALLED_ENGINE',skillflow.__file__)
n=sys.argv[1]
root=Path('/tmp/aitelier-output-migration')
files=json.loads((root/'handoff-unit-batches.json').read_text())[int(n)]
# Tests mock the search HTTP call but require a configured endpoint to reach it.
home=root/'handoff-test-home'; home.mkdir(exist_ok=True)
env=dict(os.environ, SEARXNG_URL='http://example.invalid', HOME=str(home))
log=root/f'handoff-unit-{n}.log'
with log.open('w') as f:
 result=subprocess.run([sys.executable,str(root/'reaping_pytest.py'),*files,'-q','--disable-warnings','--tb=short','--timeout=30',
                       '--junitxml='+str(root/f'handoff-unit-{n}.xml')],stdout=f,stderr=subprocess.STDOUT,env=env)
print('BATCH',n,'EXIT',result.returncode,'FILES',len(files))
print(log.read_text()[-11000:])
raise SystemExit(result.returncode)
