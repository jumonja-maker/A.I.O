"""Compose only the two literal test branches. Never QA/main or force push.

Every path is merged by Git object identity against the pinned common base.
Only the known UnoSession edits receive a textual conflict resolution; all
other conflicts abort. The result is a merge commit with both parents and is
then subject to the complete immutable integration acceptance.
"""
import base64
import json
import os
import urllib.request

REPO='jumonja-maker/A.I.O'
BASE='e80cdb84b90f2737b492185a3eb22586570c9f8c'
TARGET='test/remaining-bag-integration-20261028'
TARGET_SHA='f5d82f1a8bb7e32c61166d3fb30230813dd41798'
STAGING='test/75-to-74-owned-processes-20261028'


def api(path,data=None,method=None):
    request=urllib.request.Request('https://api.github.com/repos/'+REPO+'/'+path,
        data=None if data is None else json.dumps(data).encode('utf-8'),method=method,
        headers={'Authorization':'Bearer '+os.environ['GH_TOKEN'],'Accept':'application/vnd.github+json',
                 'Content-Type':'application/json','User-Agent':'AIO-test-integration'})
    with urllib.request.urlopen(request,timeout=120) as response:return json.load(response)


def files(commit):
    tree=api('git/commits/'+commit)['tree']['sha']
    value=api('git/trees/'+tree+'?recursive=1')
    if value.get('truncated'):raise ValueError('Incomplete tree')
    return tree,{row['path']:(row['mode'],row['sha']) for row in value['tree'] if row['type']=='blob'}


def text(blob):
    value=api('git/blobs/'+blob)
    if value.get('encoding')!='base64':raise ValueError('Unexpected blob encoding')
    return base64.b64decode(value['content']).decode('utf-8')


def uno_resolution(before):
    def replace(old,new):
        nonlocal before
        if before.count(old)!=1:raise ValueError('Unexpected UNO merge anchor: '+old[:100])
        before=before.replace(old,new)
    replace('from .dif_evidence import DIF_CORE_SOURCE\n',
            'from .dif_evidence import DIF_CORE_SOURCE\nfrom .native_profile import PrivateNativeProfile, private_profile_required\n')
    replace('        self.profile = self.pipe = None\n',
            '        self.profile = self.pipe = None\n        self._profile_owner = None\n')
    replace('            self.profile = self.workspace / secrets.token_hex(8)\n',
            "            if private_profile_required():\n                self._profile_owner = PrivateNativeProfile()\n                self.profile = self._profile_owner.path / 'p'\n            else:\n                self.profile = self.workspace / secrets.token_hex(8)\n")
    replace('        tree, reader, logs, profile = self.tree, self.reader, self.logs, self.profile\n',
            '        tree, reader, logs, profile = self.tree, self.reader, self.logs, self.profile\n        owner, self._profile_owner = self._profile_owner, None\n')
    replace('                    if profile:\n                        shutil.rmtree(profile,ignore_errors=True)\n',
            '                    if owner and stopped:\n                        owner.cleanup()\n                    elif profile and owner is None:\n                        shutil.rmtree(profile,ignore_errors=True)\n')
    compile(before,'uno_session.py','exec')
    for required in ('SHEET_OUTPUT_SOURCE','inspect_html_content_profile','export_table_sheets','PrivateNativeProfile'):
        if required not in before:raise ValueError('Lost reviewed UNO feature: '+required)
    return before


head=os.environ['AIO_COMPOSE_HEAD']
if api('git/ref/heads/'+STAGING)['object']['sha']!=head:raise ValueError('Staging lease lost')
if api('git/ref/heads/'+TARGET)['object']['sha']!=TARGET_SHA:raise ValueError('Integration target moved')
_,base=files(BASE)
_,left=files(head)
right_tree,right=files(TARGET_SHA)
changes=[];conflicts=[]
for path in sorted(set(base)|set(left)|set(right)):
    b,l,r=base.get(path),left.get(path),right.get(path)
    if l==r:chosen=l
    elif l==b:chosen=r
    elif r==b:chosen=l
    elif path=='app_backend/converter_guard/uno_session.py':
        content=uno_resolution(text(r[1]))
        changes.append({'path':path,'mode':'100644','type':'blob','content':content})
        conflicts.append({'path':path,'base':b[1],'left':l[1],'right':r[1],
                          'resolution':'retain all-sheet/HTML code; apply exclusive native profile allocation/recycle'})
        continue
    else:raise ValueError('Unreviewed integration conflict: '+path)
    if chosen!=r:
        changes.append({'path':path,'mode':(chosen or r)[0],'type':'blob','sha':chosen[1] if chosen else None})
changes.append({'path':'docs/review/evidence/00aio-finish/composition.json','mode':'100644','type':'blob',
    'content':json.dumps({'base':BASE,'left':head,'right':TARGET_SHA,'conflicts':conflicts,
                         'scope':'test branch only; acceptance required'},indent=2)})
new_tree=api('git/trees',{'base_tree':right_tree,'tree':changes})['sha']
commit=api('git/commits',{'message':'test(integration): compose native process fix with all remaining WORK features',
                        'tree':new_tree,'parents':[head,TARGET_SHA]})['sha']
if api('git/ref/heads/'+STAGING)['object']['sha']!=head:raise ValueError('Staging changed during composition')
api('git/refs/heads/'+STAGING,{'sha':commit,'force':False},method='PATCH')
print('COMPOSED_TEST_SOURCE='+commit+';TREE='+new_tree+';CONFLICTS='+str(len(conflicts)),flush=True)
