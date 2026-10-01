"""Run a real offline classroom sample through the installed application."""
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import time

root = Path(__file__).resolve().parents[1]
os.chdir(root)
os.environ['TONGJI_CLIPPER_HOME'] = str(root / '.runtime-validation')
os.environ['HF_HUB_OFFLINE'] = '1'
os.environ['PYTHONUTF8'] = '1'
from tongji_clipper.native_host.bridge import send_worker_request

probe = json.loads((root / 'artifacts/probe/latest-probe.json').read_text(encoding='utf-8-sig'))
sample = Path(probe['sample_path'])
output = root / 'artifacts/implementation'
output.mkdir(parents=True, exist_ok=True)
capture = dict(schema_version=1, tenant_code='local', course_id='12345', sub_id='67890-sample', source_url='https://look.tongji.edu.cn/livingroom?course_id=12345&sub_id=67890&tenant_code=222', course_title='示例课程', sub_title='示例课次（120–150秒验收样本）', lecturer_name='示例教师', room_name='示例教室', media_streams=[dict(stream_id='local_audio', signed_url=str(sample), time_offset=120)])
capture_path = output / 'local-sample-capture.json'
capture_path.write_text(json.dumps(capture, ensure_ascii=False), encoding='utf-8')
command = [sys.executable, '-m', 'tongji_clipper.cli', 'submit', str(capture_path), '--model', str(root / 'models/faster-whisper-small'), '--output-dir', str(output / 'transcripts'), '--offline', '--stream-id', 'local_audio', '--watch']
started = time.perf_counter()
result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', timeout=180)
print(result.stdout)
if result.returncode:
    raise RuntimeError(result.stderr or result.stdout)
jobs = send_worker_request('/jobs')['jobs']
job = next(j for j in jobs if j['sub_id'] == capture['sub_id'])
assert job['status'] == 'completed', job
job_dir = Path(job['output_dir'])
markdown = next(job_dir.glob('*.md'))
subtitle = next(job_dir.glob('*.srt'))
segments_path = next(job_dir.glob('*.segments.json'))
segments = json.loads(segments_path.read_text(encoding='utf-8'))
assert segments and all(120 <= s['start'] < s['end'] <= 150.1 for s in segments), segments
assert '#clip-t=' in markdown.read_text(encoding='utf-8')
assert '?course_id=12345&sub_id=67890&tenant_code=222' in markdown.read_text(encoding='utf-8')
assert sample.exists() and not (job_dir / 'audio_16k_mono.wav').exists()
duplicate = send_worker_request('/submit', 'POST', {'capture': capture, 'stream_id': 'local_audio', 'config': job['config']})
assert duplicate['job']['job_id'] == job['job_id'] and duplicate['job']['status'] == 'completed'
message = json.dumps({'action': 'ping'}).encode('utf-8')
bridge = subprocess.run([sys.executable, '-m', 'tongji_clipper.native_host.bridge'], input=struct.pack('<I', len(message)) + message, capture_output=True, timeout=20)
assert bridge.returncode == 0 and len(bridge.stdout) >= 4, bridge.stderr
length = struct.unpack('<I', bridge.stdout[:4])[0]
assert len(bridge.stdout) == length + 4, 'Non-protocol stdout from bridge'
reply = json.loads(bridge.stdout[4:])
assert reply['status'] == 'ok'
assert send_worker_request('/jobs/' + job['job_id'])['job']['status'] == 'completed', 'Worker did not survive bridge exit'
evidence = dict(job_id=job['job_id'], status=job['status'], offline=True, model=str(root / 'models/faster-whisper-small'), sample_duration_seconds=probe['actual_duration_seconds'], source_offset_seconds=120, wall_seconds=time.perf_counter()-started, segment_count=len(segments), markdown=str(markdown), subtitle=str(subtitle), segments=str(segments_path), idempotent_resubmission=True, native_stdio_ping=True, worker_survives_bridge_exit=True, original_audio_preserved=True, temporary_audio_removed=True, quality='ASR output, not manually corrected or accuracy certified')
(output / 'runtime-validation.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(evidence, ensure_ascii=False, indent=2))
send_worker_request('/shutdown', 'POST')
