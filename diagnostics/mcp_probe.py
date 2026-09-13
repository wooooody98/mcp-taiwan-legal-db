"""Real read-only MCP queries. Generic legal questions only; no personal data."""
from __future__ import annotations
import concurrent.futures
import importlib.metadata
import json
from pathlib import Path
import queue
import subprocess
import threading
import time
import traceback
from datetime import datetime, timezone
import requests
OUT = Path('mcp-results')
OUT.mkdir(exist_ok=True)
LOCK = threading.Lock()

def record(server, stage, payload):
    row = {'time_utc': datetime.now(timezone.utc).isoformat(), 'server': server, 'stage': stage, 'payload': payload}
    text = json.dumps(row, ensure_ascii=False, default=str)
    with LOCK:
        with (OUT / f'{server}.jsonl').open('a', encoding='utf-8') as f:
            f.write(text + '\n')
        print(text if len(text) <= 25000 else text[:25000] + ' [LOG PREVIEW ONLY; complete response in artifact]', flush=True)

def decode_tool(response):
    r = response.get('result', response)
    if isinstance(r, dict):
        if isinstance(r.get('structuredContent'), dict):
            return r['structuredContent']
        for part in r.get('content', []):
            if part.get('type') == 'text':
                try:
                    return json.loads(part['text'])
                except (ValueError, TypeError):
                    pass
    return r

class HTTPMCP:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream'})
        self.url = 'https://tlr.dr-legal.com.tw/mcp'
        self.n = 0
    def send(self, method, params=None, notification=False):
        self.n += 1
        body = {'jsonrpc': '2.0', 'method': method}
        if not notification:
            body['id'] = self.n
        if params is not None:
            body['params'] = params
        record('tw-legal-rag', 'request', body)
        with self.s.post(self.url, json=body, timeout=(15, 90), stream=True) as r:
            if r.headers.get('Mcp-Session-Id'):
                self.s.headers['Mcp-Session-Id'] = r.headers['Mcp-Session-Id']
            r.raise_for_status()
            if notification:
                return {'http_status': r.status_code}
            if 'text/event-stream' not in r.headers.get('Content-Type', ''):
                data = r.json()
                record('tw-legal-rag', 'response', data)
                return data
            accumulated = []
            for line in r.iter_lines(decode_unicode=True):
                if isinstance(line, bytes):
                    line = line.decode('utf-8')
                if line.startswith('data:'):
                    accumulated.append(line[5:].lstrip())
                elif not line and accumulated:
                    data = json.loads('\n'.join(accumulated))
                    accumulated = []
                    if data.get('id') == body['id']:
                        record('tw-legal-rag', 'response', data)
                        return data
            raise RuntimeError('SSE ended before matching JSON-RPC response')

class StdioMCP:
    def __init__(self):
        self.q = queue.Queue()
        self.p = subprocess.Popen(['mcp-taiwan-legal-db'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        self.n = 0
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._stderr, daemon=True).start()
    def _read(self):
        for line in self.p.stdout:
            try:
                self.q.put(json.loads(line))
            except ValueError:
                record('mcp-taiwan-legal-db', 'non_json_stdout', line)
        self.q.put({'process_ended': True, 'returncode': self.p.poll()})
    def _stderr(self):
        for line in self.p.stderr:
            with LOCK:
                with (OUT / 'mcp-taiwan-legal-db.stderr.log').open('a', encoding='utf-8') as f:
                    f.write(line)
    def send(self, method, params=None, notification=False):
        self.n += 1
        body = {'jsonrpc': '2.0', 'method': method}
        if not notification:
            body['id'] = self.n
        if params is not None:
            body['params'] = params
        record('mcp-taiwan-legal-db', 'request', body)
        self.p.stdin.write(json.dumps(body, ensure_ascii=False) + '\n')
        self.p.stdin.flush()
        if notification:
            return {'notification_sent': True}
        deadline = time.monotonic() + 110
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f'No MCP response for {method}')
            msg = self.q.get(timeout=remaining)
            if msg.get('process_ended'):
                raise RuntimeError(f'MCP process exited: {msg}')
            if msg.get('id') == body['id']:
                record('mcp-taiwan-legal-db', 'response', msg)
                return msg
            record('mcp-taiwan-legal-db', 'notification_or_other_response', msg)
    def close(self):
        self.p.terminate()
        try:
            self.p.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self.p.kill()
            self.p.wait()

def init(client, server):
    response = client.send('initialize', {'protocolVersion': '2025-03-26', 'capabilities': {}, 'clientInfo': {'name': 'tw-legal-mcp-verification', 'version': '1.0.0'}})
    if response.get('error'):
        raise RuntimeError(response['error'])
    version = response.get('result', {}).get('protocolVersion')
    if isinstance(client, HTTPMCP) and version:
        client.s.headers['MCP-Protocol-Version'] = version
    client.send('notifications/initialized', notification=True)
    listing = client.send('tools/list', {})
    schemas = {t['name']: t.get('inputSchema', {}) for t in listing.get('result', {}).get('tools', [])}
    record(server, 'discovered_tool_names', list(schemas))
    return schemas

def call(client, server, tool, args):
    try:
        response = client.send('tools/call', {'name': tool, 'arguments': args})
        data = decode_tool(response)
        record(server, 'decoded_tool_result', {'tool': tool, 'arguments': args, 'data': data})
        return data
    except Exception as exc:
        record(server, 'call_error', {'tool': tool, 'arguments': args, 'error': repr(exc)})
        return None

def query_args(schema, query, max_results=5, read_top=3):
    props = schema.get('properties', {})
    key = next((k for k in ('query', 'question', 'keyword') if k in props), 'query')
    args = {key: query}
    if 'max_results' in props:
        args['max_results'] = max_results
    if 'read_top' in props:
        args['read_top'] = read_top
    return args

def run_remote():
    server = 'tw-legal-rag'
    client = HTTPMCP()
    try:
        schemas = init(client, server)
        if 'get_legal_reference' in schemas:
            call(client, server, 'get_legal_reference', {'serial': '勞動2字第1020083156號'})
        tool = 'search_bundle' if 'search_bundle' in schemas else 'search_judgments'
        if tool in schemas:
            call(client, server, tool, query_args(schemas[tool], '月薪制勞工月中到職，勞動契約未約定未足月工資換算分母。法院如何判斷按30日或當月實際31日計算？民法第123條及公司計薪慣例。'))
        if 'search_judgments' in schemas:
            call(client, server, 'search_judgments', query_args(schemas['search_judgments'], '臺北高等行政法院110年度訴字第1335號', max_results=2, read_top=2))
        if tool in schemas:
            call(client, server, tool, query_args(schemas[tool], '新進勞工第一次領薪立即提出異議，公司長期薪資計算慣例但未告知，是否構成默示合意或企業習慣？'))
    except Exception:
        record(server, 'fatal_error', traceback.format_exc())
    finally:
        client.s.close()
        record(server, 'completed', True)

def run_local():
    server = 'mcp-taiwan-legal-db'
    client = None
    try:
        record(server, 'installed_distribution_version', importlib.metadata.version('mcp-taiwan-legal-db'))
        client = StdioMCP()
        schemas = init(client, server)
        call(client, server, 'query_regulation', {'law_name': '民法', 'article_no': '123'})
        data = call(client, server, 'search_judgments', {'court': '臺北高等行政法院', 'case_type': '行政', 'year_from': 110, 'case_word': '訴', 'case_number': '1335', 'max_results': 3})
        if isinstance(data, dict):
            for row in data.get('results', [])[:1]:
                if isinstance(row, dict) and row.get('jid'):
                    call(client, server, 'get_judgment', {'jid': row['jid']})
        data = call(client, server, 'search_judgments', {'keyword': '破月', 'case_type': '民事', 'max_results': 5})
        if isinstance(data, dict):
            for row in data.get('results', [])[:2]:
                if isinstance(row, dict) and row.get('jid'):
                    call(client, server, 'get_judgment', {'jid': row['jid']})
    except Exception:
        record(server, 'fatal_error', traceback.format_exc())
    finally:
        if client:
            client.close()
        record(server, 'completed', True)

if __name__ == '__main__':
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        tasks = [pool.submit(run_remote), pool.submit(run_local)]
        for task in tasks:
            task.result()
    print('MCP_TEST_FINISHED; distinguish successful calls from returned errors in artifacts.', flush=True)
