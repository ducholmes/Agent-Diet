import json
import multiprocessing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from openhands_adapter.events import configure, emit, read_events, read_events_strict, audit_errors
from openhands_adapter.compat.audit import byte_hash, canonical_json_bytes, retention_copy, register_secrets, payload_hash


def writer(path, number):
    configure(Path(path),run_id='run',case_id='case',producer=f'writer-{number}')
    for i in range(8):emit('large_record',payload={'text':('Unicode lỗi\n'*12000)+str(i)})


class IntegrityTests(unittest.TestCase):
    def tearDown(self):configure(None)

    def test_A14_13_concurrent_large_records(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'events.jsonl'; path.touch()
            ctx=multiprocessing.get_context('fork')
            processes=[ctx.Process(target=writer,args=(str(path),i)) for i in range(4)]
            for p in processes:p.start()
            for p in processes:p.join(30);self.assertEqual(p.exitcode,0)
            records,issues=read_events_strict(path)
            self.assertEqual(issues,[]);self.assertEqual(len(records),32)
            self.assertTrue(all(payload_hash(r['payload']) == r['payload_sha256'] for r in records))

    def test_A14_14_15_tail_middle_duplicates_sequence_schema(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'events.jsonl';configure(path,reset=True)
            emit('first');emit('last');original=path.read_bytes();lines=original.splitlines(keepends=True)
            path.write_bytes(original+b'{partial')
            records,issues=read_events_strict(path);self.assertEqual(len(records),2);self.assertTrue(issues[0].startswith('truncated_tail'))
            self.assertEqual(len(read_events(path)),2)
            path.write_bytes(lines[0]+b'{bad}\n'+lines[1]);self.assertRaises(ValueError,read_events,path)
            self.assertTrue(any(i.startswith('corrupt_record') for i in read_events_strict(path)[1]))
            path.write_bytes(lines[0]+lines[0]+lines[1]);self.assertTrue(any(i.startswith('duplicate_record') for i in read_events_strict(path)[1]))
            record=json.loads(lines[0]);record['type']='conflict'
            path.write_bytes(lines[0]+json.dumps(record).encode()+b'\n');self.assertTrue(any(i.startswith('duplicate_conflict') for i in read_events_strict(path)[1]))
            record=json.loads(lines[1]);record['schema_version']=99
            path.write_bytes(json.dumps(record).encode()+b'\n');issues=read_events_strict(path)[1]
            self.assertTrue(any(i.startswith('sequence_gap') for i in issues));self.assertTrue(any(i.startswith('unsupported_schema') for i in issues))
            for updates, issue in (({'schema_version':True},'unsupported_schema'),
                                   ({'producer':[]},'invalid_producer'),
                                   ({'record_id':{}},'missing_record_id'),
                                   ({'record_id':'other:1'},'record_id_mismatch')):
                record={**json.loads(lines[0]),**updates}
                path.write_text(json.dumps(record)+'\n')
                self.assertTrue(any(i.startswith(issue) for i in read_events_strict(path)[1]),updates)

    def test_A14_17_audit_write_and_serialization_do_not_raise(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'events.jsonl';configure(path,reset=True)
            emit('unsupported',payload=object())
            with patch('openhands_adapter.events.os.write',side_effect=OSError('disk full')):emit('write_failure')
            emit('next',payload='preserved')
            self.assertEqual(len(audit_errors()),2)
            self.assertEqual(read_events_strict(path)[1],[])
            self.assertEqual(read_events(path)[0]['producer_seq'],1)

    def test_A14_02_18_hash_copy_keeps_wire_and_redacts_export(self):
        secret='sentinel-credential-12345';register_secrets(secret)
        payload={'messages':[{'role':'user','content':'Lỗi\n'*20000+secret}], 'arguments':' { raw : invalid } '}
        original=canonical_json_bytes(payload);raw=retention_copy({'payload':payload},True)
        hashed=retention_copy({'payload':payload},False)
        self.assertEqual(raw['payload_sha256'],byte_hash(original));self.assertEqual(hashed['payload_sha256'],raw['payload_sha256'])
        self.assertTrue(raw['payload_redacted']);self.assertNotIn(secret,json.dumps(raw));self.assertNotIn('payload',hashed)
        self.assertIn(secret,canonical_json_bytes(payload).decode())
