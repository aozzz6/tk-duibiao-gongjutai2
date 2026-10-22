"""
batch_transcribe_fw.py — Step5 批量转写（v3：GPU 优先，CPU 兜底）

输入: --audio-dir
输出: transcripts/{video_id}_transcript.txt（带时间戳分段）
      + .transcribe_state.json 断点续传
      + stdout 末行 JSON 摘要（对齐 gate_transcribe）:
        {"total": n, "done": [ids], "suspect_hallucination": [{"id","text"}]}
门禁阈值: 单条全文 <30 字符 → 疑似幻觉/纯 BGM，标记不盲信

★ v3（2026-10-14）：启用 GPU（RTX 2060 / ctranslate2 CUDA）。
  实测 medium 模型「GPU fp16」≈10.6x 实时，CPU PyTorch fp32 仅 ≈1.2x → 约 7 倍提速
  （410s 音频：280s → 39s）。`--backend auto`（新默认）= 有 CUDA 设备走 faster+CUDA，
  否则退回 openai/PyTorch CPU。
  依赖：`nvidia-cudnn-cu12`(9.x) + `nvidia-cublas-cu12` —— ct2 的 Windows 轮子自带
  cudnn64_9.dll 但缺 cudnn_ops/heuristic 等，必须把它们的 bin 目录加进 **系统 PATH**
  （仅 os.add_dll_directory 不够：ct2 是 C++ 侧 LoadLibrary，只认 PATH）。
  ⚠ 本机 onnxruntime 段错误（干净 venv 里 import 都崩）→ VAD 不可用，故 faster 后端
  一律 `vad_filter=False`；BatchedInferencePipeline 同样不可用。
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import argparse
import datetime as dt
import glob
import json
import os
import sys
from pathlib import Path
os.environ.setdefault('HF_HUB_OFFLINE', '1')
AUDIO_EXTS = ('.mp3', '.m4a', '.wav')
HALLUCINATION_MIN_CHARS = 30
SIMPLIFIED_PROMPT = '以下是普通话的句子。'
_DLL_HANDLES = []

def setup_cuda_dll_paths() -> list[str]:
    """把 site-packages/nvidia/*/bin 加进 PATH（ct2 在 C++ 侧 LoadLibrary，只认 PATH）。

    返回已加入的目录列表（便于日志/排错）。"""
    added = []
    try:
        import site
        roots = list(site.getsitepackages())
    except Exception:
        roots = []
    roots.append(str(Path(sys.prefix) / 'Lib' / 'site-packages'))
    for root in roots:
        for d in glob.glob(os.path.join(root, 'nvidia', '*', 'bin')):
            if os.path.isdir(d):
                os.environ['PATH'] = d + os.pathsep + os.environ.get('PATH', '')
                try:
                    _DLL_HANDLES.append(os.add_dll_directory(d))
                except Exception:
                    pass
                added.append(d)
    return added

def cuda_device_count() -> int:
    """ct2 可见的 CUDA 设备数（0 = 走 CPU 兜底）。"""
    try:
        import ctranslate2
        return int(ctranslate2.get_cuda_device_count())
    except Exception:
        return 0

def fmt_ts(sec: float) -> str:
    ms = int(sec * 1000)
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f'{h:02d}:{m:02d}:{s:02d},{ms:03d}'

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--audio-dir', required=True)
    ap.add_argument('--model', default='medium')
    ap.add_argument('--compute-type', default='int8', help='CPU 路径量化类型')
    ap.add_argument('--gpu-compute-type', default='float16', help='GPU 路径精度')
    ap.add_argument('--lang', default='zh')
    ap.add_argument('--simplified', action='store_true', help='加 initial_prompt 偏置简体中文')
    ap.add_argument('--backend', choices=['auto', 'faster', 'openai'], default='auto', help='auto=有 CUDA 设备走 faster+GPU，否则 openai/PyTorch CPU（推荐）；faster=faster-whisper/ctranslate2；openai=openai-whisper/PyTorch')
    ap.add_argument('--device', choices=['auto', 'cpu', 'cuda'], default='auto', help='auto=有 CUDA 设备即用 GPU（仅 faster 后端生效）')
    ap.add_argument('--vad', choices=['off', 'on'], default='off', help='⚠ off 为默认：本机 onnxruntime 段错误，vad_filter=True 必崩')
    ap.add_argument('--model-dir', default=None, help='本地 CTranslate2 模型目录（faster_whisper 离线加载，绕过 HF 下载；如 C:\\...\\faster-whisper\\medium-ct2）。缺省时按 --model 名联网拉取。')
    a = ap.parse_args()
    a.lang = {'jp': 'ja', 'kr': 'ko'}.get(a.lang, a.lang)
    audio_dir = Path(a.audio_dir)
    if not audio_dir.is_dir():
        ap.error(f'音频目录不存在: {audio_dir}')
    tr_dir = audio_dir / 'transcripts'
    tr_dir.mkdir(parents=True, exist_ok=True)
    state_path = tr_dir / '.transcribe_state.json'
    state = {}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding='utf-8'))
            if not isinstance(state, dict) or not isinstance(state.get('done', {}), dict) or (not isinstance(state.get('suspect_hallucination', []), list)):
                raise ValueError('断点文件结构不符')
        except (ValueError, OSError) as e:
            print(f'[error] 无法读取断点文件，未覆盖: {e}', file=sys.stderr)
            return 1
    done = state.get('done', {})
    suspect = state.get('suspect_hallucination', [])
    _PREF = {'.mp3': 0, '.m4a': 1, '.wav': 2}
    _uniq: dict = {}
    for p in sorted(audio_dir.iterdir(), key=lambda x: x.name):
        if not p.is_file() or p.suffix.lower() not in AUDIO_EXTS or p.stat().st_size <= 0:
            continue
        cur = _uniq.get(p.stem)
        if cur is None or _PREF.get(p.suffix.lower(), 9) < _PREF.get(cur.suffix.lower(), 9):
            _uniq[p.stem] = p
    files = sorted(_uniq.values(), key=lambda p: p.name)
    total = len(files)
    current_ids = {p.stem for p in files}
    done = {vid: record for vid, record in done.items() if vid in current_ids and (tr_dir / f'{vid}_transcript.txt').is_file() and ((tr_dir / f'{vid}_transcript.txt').stat().st_size > 0)}
    suspect = [s for s in suspect if isinstance(s, dict) and s.get('id') in current_ids]
    print(f'[info] 共 {total} 个音频（已按 video_id 去重；目录文件 {len(list(audio_dir.iterdir()))}），已完成 {len(done)}（断点续传）', file=sys.stderr)
    if len(done) < total:
        added = setup_cuda_dll_paths()
        n_gpu = cuda_device_count() if a.device != 'cpu' and os.environ.get('WB_FASTER_MODEL_DIR') else 0
        if not a.model_dir:
            a.model_dir = os.environ.get('WB_FASTER_MODEL_DIR') or None
        use_cuda = n_gpu > 0 and a.backend in ('auto', 'faster') and (a.device in ('auto', 'cuda'))
        backend = 'faster' if use_cuda or a.backend == 'faster' else 'openai'
        if a.backend == 'auto' and (not use_cuda):
            backend = 'openai'
        print(f'[info] backend={backend} cuda_devices={n_gpu} compute_type={(a.gpu_compute_type if use_cuda else a.compute_type)} vad={a.vad} nvidia_dll_dirs={len(added)}', file=sys.stderr)
        if backend == 'openai':
            import whisper
            model_file = os.environ['WB_WHISPER_MODEL']
            if not Path(model_file).is_file():
                raise RuntimeError('转写模型文件不存在，请在工作台设置中指定 .pt 模型')
            owm = whisper.load_model(model_file, device='cpu', download_root=os.environ['WB_WHISPER_CACHE'])

            def _trans(p):
                res = owm.transcribe(str(p), language=a.lang or None, initial_prompt=SIMPLIFIED_PROMPT if a.simplified else None, verbose=False)
                lines, text_all = ([], [])
                for s in res['segments']:
                    lines.append(f"[{fmt_ts(s['start'])} -> {fmt_ts(s['end'])}] {s['text'].strip()}")
                    text_all.append(s['text'].strip())
                return (lines, ''.join(text_all), float(res.get('duration') or 0))
        else:
            try:
                from faster_whisper import WhisperModel
                ctype = a.gpu_compute_type if use_cuda else a.compute_type
                model_src = a.model_dir if a.model_dir else a.model
                model = WhisperModel(model_src, device='cuda' if use_cuda else 'cpu', compute_type=ctype)
            except Exception as e:
                print(f'[warn] faster 后端初始化失败（{e.__class__.__name__}: {str(e)[:120]}），自动回退 openai CPU 后端', file=sys.stderr)
                backend = 'openai'
                import whisper
                model_file = os.environ['WB_WHISPER_MODEL']
                if not Path(model_file).is_file():
                    raise RuntimeError('转写模型文件不存在，请在工作台设置中指定 .pt 模型')
                owm = whisper.load_model(model_file, device='cpu', download_root=os.environ['WB_WHISPER_CACHE'])

                def _trans(p):
                    res = owm.transcribe(str(p), language=a.lang or None, initial_prompt=SIMPLIFIED_PROMPT if a.simplified else None, verbose=False)
                    lines, text_all = ([], [])
                    for s in res['segments']:
                        lines.append(f"[{fmt_ts(s['start'])} -> {fmt_ts(s['end'])}] {s['text'].strip()}")
                        text_all.append(s['text'].strip())
                    return (lines, ''.join(text_all), float(res.get('duration') or 0))
            else:

                def _trans(p):
                    seg_iter, info = model.transcribe(str(p), language=a.lang, vad_filter=a.vad == 'on', initial_prompt=SIMPLIFIED_PROMPT if a.simplified else None)
                    lines, text_all = ([], [])
                    for s in seg_iter:
                        lines.append(f'[{fmt_ts(s.start)} -> {fmt_ts(s.end)}] {s.text.strip()}')
                        text_all.append(s.text.strip())
                    return (lines, ''.join(text_all), float(info.duration or 0))
        for p in files:
            vid = p.stem
            if vid in done:
                continue
            suspect = [s for s in suspect if s.get('id') != vid]
            t0 = dt.datetime.now()
            try:
                lines, full, dur = _trans(p)
                (tr_dir / f'{vid}_transcript.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8')
                done[vid] = {'file': p.name, 'chars': len(full), 'seconds': round(dur, 1)}
                if len(full) < HALLUCINATION_MIN_CHARS:
                    suspect.append({'id': vid, 'text': full[:40]})
            except Exception as e:
                suspect.append({'id': vid, 'text': f'TRANSCRIBE_ERROR: {e}'[:60]})
            cost = (dt.datetime.now() - t0).total_seconds()
            print(f'[info] {vid}: {len(done)}/{total} 耗时 {cost:.0f}s', file=sys.stderr)
            state.update({'done': done, 'suspect_hallucination': suspect})
            tmp = state_path.with_suffix('.json.tmp')
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
            tmp.replace(state_path)
    else:
        print('[info] 全部已完成，直接出摘要', file=sys.stderr)
    summary = {'total': total, 'done': list(done.keys()), 'suspect_hallucination': suspect}
    state.update({'done': done, 'suspect_hallucination': suspect})
    tmp = state_path.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(state_path)
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if len(done) == total else 1
if __name__ == '__main__':
    sys.exit(main())
