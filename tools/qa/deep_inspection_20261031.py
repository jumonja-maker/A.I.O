"""Correct audit orchestration only; immutable product code remains untouched.
The prior bare-node guard_queue fixture lacked its required HTTP server URL.
That was an audit setup error, NOT an application defect. Run its real driver.
"""
from pathlib import Path
import subprocess
ROOT=Path(__file__).resolve().parents[2]
REPLAY='ce3ee62133d207b69777ba6bf6f5edb8ec418dc8'
source=subprocess.check_output(['git','show',REPLAY+':tools/qa/deep_inspection_20261031.py'],cwd=ROOT).decode()
old="names=('legacy_recovery_ui.mjs','conversion_history_dom.mjs','guard_ui_lifecycle_v20.mjs','guard_queue_dom_v08.mjs')"
new="names=('legacy_recovery_ui.mjs','conversion_history_dom.mjs','guard_ui_lifecycle_v20.mjs')"
assert source.count(old)==1
source=source.replace(old,new)
anchor="    return {'issue_reproduced':any(r['exit'] for r in results),'control_pass':all(r['exit']==0 for r in results),'cases':results,"
replacement="""    drivers=[p for p in (ROOT/'tests').glob('test_*.py')
             if 'guard_queue_dom_v08.mjs' in p.read_text(encoding='utf-8')]
    assert drivers, 'No Python HTTP fixture driver found; cannot claim the test ran'
    for driver in drivers:
        result=subprocess.run([sys.executable,'-m','unittest','discover','-s','tests','-p',driver.name,'-v'],
                              cwd=ROOT,capture_output=True,text=True,timeout=120)
        results.append({'file':driver.name,'role':'real Python server fixture driver',
                        'exit':result.returncode,'stdout':result.stdout[-4000:],'stderr':result.stderr[-4000:]})
    return {'issue_reproduced':any(r['exit'] for r in results),'control_pass':all(r['exit']==0 for r in results),'cases':results,"""
assert source.count(anchor)==1
source=source.replace(anchor,replacement)
exec(compile(source,'immutable-audit-with-correct-http-driver','exec'),globals())
