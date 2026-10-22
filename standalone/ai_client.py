"""Provider-neutral chat-completions client using only this app's settings."""
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def model_config(vision=False):
    settings = json.loads((Path(os.environ['WB_PROFILE_ROOT']) / 'settings.json').read_text(encoding='utf-8'))
    prefix = 'vision' if vision else 'ai'
    base = str(settings.get(prefix + 'BaseUrl') or '').strip().rstrip('/')
    name = str(settings.get(prefix + 'Model') or '').strip()
    key = str(settings.get(prefix + 'ApiKey') or '').strip()
    parsed = urllib.parse.urlsplit(base)
    if not base or not name or parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise ValueError('请在工作台设置中填写' + ('视觉' if vision else '文本') + '模型 API 地址和模型名称')
    if not key and parsed.hostname not in ('localhost', '127.0.0.1', '::1'):
        raise ValueError('远程模型服务需要填写 API Key')
    return {'url': base, 'id': name, 'apiKey': key}


def completion(system, user, model=None, max_tokens=None, timeout=180):
    model = model or model_config()
    base = model['url'].rstrip('/')
    endpoint = base if base.endswith('/chat/completions') else base + '/chat/completions'
    payload = {'model': model['id'], 'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}]}
    if max_tokens is not None:
        payload['max_tokens'] = max_tokens
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    headers = {'Content-Type': 'application/json'}
    if model['apiKey']:
        headers['Authorization'] = 'Bearer ' + model['apiKey']
    req = urllib.request.Request(endpoint, data=body, headers=headers)
    # A TikTok collection proxy must not silently redirect model API traffic.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as response:
            raw = response.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise ValueError('模型返回内容过大，已停止处理')
        result = json.loads(raw.decode('utf-8'))
        text = result['choices'][0]['message']['content']
        if not isinstance(text, str) or not text.strip():
            raise ValueError('模型未返回文本内容')
        return text.strip()
    except urllib.error.HTTPError as exc:
        # Do not print provider bodies, headers, or keys into shared task logs.
        raise RuntimeError(f'模型 API 返回 HTTP {exc.code}，请检查地址、模型名称和权限') from None
    except urllib.error.URLError:
        raise RuntimeError('无法连接模型服务，请检查 API 地址与网络') from None
    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
        raise RuntimeError('模型服务返回格式不符合 chat-completions 文本协议') from None
