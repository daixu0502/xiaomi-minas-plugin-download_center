import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'payload/files'))
from download_lib import Manager
from dual_engine import DualManager


class PerformanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.home=Path(self.tmp.name);(self.home/'var').mkdir();(self.home/'data').mkdir()
        self.m=DualManager(self.home,self.home/'data',False)

    def insert(self,ident,status='waiting'):
        snap=dict(engine='qbittorrent',status=status,initializing=False,name='test',directory='Download',addedAt=0)
        with self.m.db() as db:db.execute('INSERT INTO tasks VALUES (?,?,?,?,?)',(ident,ident,0,'{}',json.dumps(snap)))

    def test_settled_task_does_not_read_source(self):
        snap={'initializing':False}
        with patch.object(self.m,'row',side_effect=AssertionError('unneeded source read')):
            self.assertIs(self.m.prepare_task({}, {}, snap),snap)

    def test_stale_sample_never_changes_new_changed_or_deleted_tasks(self):
        self.insert('a'*40)
        captured=self.m.rows()
        remote={'known':{r['id']:(r['gid'],r['snapshot']) for r in captured},'aria':[],'qb':[]}
        with self.m.db() as db:
            snap=json.loads(captured[0]['snapshot']);snap['status']='paused'
            db.execute('UPDATE tasks SET snapshot=?',(json.dumps(snap),))
        self.insert('b'*40)
        self.m.sync(remote=remote)
        self.assertEqual(json.loads(self.m.row('a'*40)['snapshot'])['status'],'paused')
        self.assertEqual(json.loads(self.m.row('b'*40)['snapshot'])['status'],'waiting')
        with self.m.db() as db:db.execute('DELETE FROM tasks WHERE id=?',('a'*40,))
        self.m.sync(remote=remote)
        self.assertEqual([r['id'] for r in self.m.rows()],['b'*40])

    def test_batch_updates_use_one_write_transaction(self):
        self.insert('a'*40);self.insert('b'*40)
        items=[dict(hash=c*40,state='downloading',name='test',size=10,amount_left=9,dlspeed=1) for c in 'ab']
        statements=[];original=self.m.db
        def database():
            db=original();db.set_trace_callback(statements.append);return db
        with patch.object(self.m,'db',side_effect=database),patch.object(self.m.qb,'torrents',return_value=items),patch.object(Manager,'sync'):
            self.m.sync()
        self.assertEqual(sum(s.startswith('BEGIN') for s in statements),1)
        self.assertEqual(sum(s.startswith('UPDATE tasks') for s in statements),2)

    def test_static_cache_invalidates_on_restart(self):
        calls=[]
        def load():calls.append(1);return 'version'
        self.m.runtime_value('test',load);self.m.runtime_value('test',load)
        self.assertEqual(len(calls),1)
        (self.m.var/'service.pid').write_text('newpid')
        self.m.runtime_value('test',load);self.assertEqual(len(calls),2)

    def test_lightweight_status_omits_settings_and_caches_versions(self):
        m=Manager(self.home,self.home/'data',False)
        def rpc(name,*args):return {'version':'1.37.0'} if name=='getVersion' else {}
        with patch.object(m,'rpc',side_effect=rpc) as request,patch.object(m,'bt_state',return_value={}),patch.object(m,'core_state',return_value={}):
            self.assertNotIn('settings',m.snapshot(False));self.assertIn('settings',m.snapshot())
        self.assertEqual(sum(c.args[0]=='getVersion' for c in request.call_args_list),1)


if __name__=='__main__':unittest.main()
