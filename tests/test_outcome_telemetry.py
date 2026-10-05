#!/usr/bin/env python3
"""Offline suite: prospective per-turn outcome telemetry (v0.3.0).

Run from the repository root::

    python3 tests/test_outcome_telemetry.py

The suite drives the real plugin handlers and the real ``router.outcome`` module against a
throwaway tree. A hostile parent environment (HERMES_HOME, HERMES_STATE_DB, HERMES_USAGE_DB,
JEV_ROUTER_ROOT, JEV_LOG_DIR, JEV_SKIP_COUNTER_PATH, JEV_DEPLOYMENT_GENERATION,
JEV_AVAILABLE_ROUTES) points at a *sentinel* production-like deployment — log tree, counter,
state database, usage database, runtime tree, plugin tree — and the suite proves that nothing
it does reads or writes those paths. Every telemetry byte produced here lands in the temp tree.

Groups
    A  usage arithmetic and the record schema
    B  error, retry, then success still completes
    C  an error alone never fabricates a terminal
    D  interruption finalizes only when the turn is unambiguous
    E  duplicate terminal hook yields exactly one record
    F  duplicate response hook is not counted twice
    G  a late response hook never rewrites a finished record
    H  two simultaneous turns in one session do not contaminate each other
    I  two simultaneous sessions stay separate
    J  content blindness: sentinels never reach any output, and no content is hashed
    K  the human-turn boundary is reused, and rejection counters are unchanged
    L  bounded memory: eviction never fabricates a terminal
    M  append-only writing, restrictive permissions, no truncation
    N  the failure policy cannot reach a Hermes turn, and analytics see a missing terminal
    O  the analyzer's prospective aggregate, incl. rejection of unexpected fields
"""
import hashlib
import importlib.util
import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
TOOL = ROOT / 'tools' / 'outcome_signals.py'
GEN = 'fixture-generation'
GENERATION_STAMP = GEN

R = []
FAILS = []

PROMPT_SENTINEL = 'SECRET_PROMPT_SENTINEL'
RESPONSE_SENTINEL = 'SECRET_RESPONSE_SENTINEL'
TOOL_ARGS_SENTINEL = 'SECRET_TOOL_ARGS_SENTINEL'
TOOL_RESULT_SENTINEL = 'SECRET_TOOL_RESULT_SENTINEL'
ERROR_MESSAGE_SENTINEL = 'SECRET_ERROR_MESSAGE_SENTINEL'
ALL_SENTINELS = (PROMPT_SENTINEL, RESPONSE_SENTINEL, TOOL_ARGS_SENTINEL, TOOL_RESULT_SENTINEL,
                 ERROR_MESSAGE_SENTINEL)


def chk(name, cond, extra=''):
    R.append((name, bool(cond), extra))
    if not cond:
        FAILS.append(name)


def fingerprint(path):
    p = pathlib.Path(path)
    if not p.exists():
        return None
    if p.is_dir():
        return {str(q.relative_to(p)): (q.stat().st_size, int(q.stat().st_mtime),
                                        hashlib.sha256(q.read_bytes()).hexdigest()[:16])
                for q in sorted(p.rglob('*')) if q.is_file()}
    return (p.stat().st_size, int(p.stat().st_mtime), hashlib.sha256(p.read_bytes()).hexdigest())


def build_state_db(path, sessions):
    con = sqlite3.connect(path)
    con.executescript("""create table sessions (id text primary key, source text);""")
    for sid in sessions:
        con.execute('insert into sessions values (?,?)', (sid, 'feishu'))
    con.commit()
    con.close()


# --------------------------------------------------------------------------- hook payloads
def pre(turn_id, session, *, api_request_id='r1', api_call_count=1, retry_count=0,
        model='deepseek-flash', platform='feishu', origin='user', content=True):
    """A payload shaped like the real ``pre_api_request`` kwargs (see the source audit)."""
    kw = {'turn_id': turn_id, 'task_id': 'task-1', 'api_request_id': api_request_id,
          'session_id': session, 'platform': platform, 'turn_origin': origin, 'model': model,
          'provider': 'deepseek', 'base_url': 'https://api.example.invalid', 'api_mode': 'chat',
          'api_call_count': api_call_count, 'retry_count': retry_count, 'message_count': 4,
          'tool_count': 12, 'approx_input_tokens': 2048, 'request_char_count': 8192,
          'max_tokens': 4096, 'started_at': time.time()}
    if content:
        kw.update({'user_message': PROMPT_SENTINEL,
                   'conversation_history': [{'role': 'user', 'content': PROMPT_SENTINEL}],
                   'request_messages': [{'role': 'tool', 'content': TOOL_RESULT_SENTINEL}],
                   'system_prompt': PROMPT_SENTINEL,
                   'request': {'method': 'POST', 'body': {'tool_args': TOOL_ARGS_SENTINEL}}})
    return kw


def post(turn_id, session, *, api_request_id='r1', api_call_count=1, input_tokens=1000,
         output_tokens=100, cache_read=800, cache_write=0, duration=1.0, finish_reason='stop',
         model='deepseek-flash', platform='feishu', content=True):
    kw = {'turn_id': turn_id, 'task_id': 'task-1', 'api_request_id': api_request_id,
          'session_id': session, 'platform': platform, 'model': model, 'provider': 'deepseek',
          'api_mode': 'chat', 'api_call_count': api_call_count, 'api_duration': duration,
          'started_at': time.time() - duration, 'ended_at': time.time(),
          'finish_reason': finish_reason, 'message_count': 4, 'response_model': model,
          'usage': {'input_tokens': input_tokens, 'output_tokens': output_tokens,
                    'cache_read_tokens': cache_read, 'cache_write_tokens': cache_write,
                    'reasoning_tokens': 0, 'request_count': 1,
                    'prompt_tokens': input_tokens + cache_read + cache_write,
                    'total_tokens': input_tokens + cache_read + cache_write + output_tokens},
          'assistant_content_chars': 1200, 'assistant_tool_call_count': 0}
    if content:
        kw.update({'response': {'assistant_message': {'role': 'assistant',
                                                      'content': RESPONSE_SENTINEL,
                                                      'tool_calls': [{'args': TOOL_ARGS_SENTINEL}]}},
                   'assistant_message': type('M', (), {'content': RESPONSE_SENTINEL,
                                                       'tool_calls': []})()})
    return kw


def err(turn_id, session, *, api_request_id='r1', retry_count=1, duration=0.2,
        platform='feishu', content=True):
    kw = {'turn_id': turn_id, 'task_id': 'task-1', 'api_request_id': api_request_id,
          'session_id': session, 'platform': platform, 'model': 'deepseek-flash',
          'api_call_count': 1, 'api_duration': duration, 'retry_count': retry_count,
          'max_retries': 3, 'retryable': True, 'status_code': 429, 'reason': 'rate_limit',
          'error': {'type': 'RateLimitError', 'message': ERROR_MESSAGE_SENTINEL},
          'request': {'method': 'POST', 'body': {'content': PROMPT_SENTINEL}}}
    return kw


def final(turn_id, session, *, model='deepseek-flash', platform='feishu', content=True):
    kw = {'turn_id': turn_id, 'task_id': 'task-1', 'session_id': session, 'model': model,
          'platform': platform}
    if content:
        kw.update({'user_message': PROMPT_SENTINEL, 'assistant_response': RESPONSE_SENTINEL,
                   'conversation_history': [{'role': 'tool', 'content': TOOL_RESULT_SENTINEL}]})
    return kw


def stopped(session_key, *, platform='feishu'):
    return {'session_key': session_key, 'platform': platform, 'reason': 'user_stop',
            'invalidation_reason': 'stop_command'}


def main():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix='jev-outcome-telemetry-'))
    fixture_logs = tmp / 'logs'
    fixture_home = tmp / 'hermes_home'
    fixture_logs.mkdir(parents=True)
    fixture_home.mkdir(parents=True)

    # ---- sentinel: a production-like deployment this suite must never touch -------------
    sentinel = tmp / 'sentinel'
    for sub in ('logs/router/outcomes', 'logs/router', 'state', 'runtime/router', 'plugins/jev-shadow-router',
                'db', 'usage'):
        (sentinel / sub).mkdir(parents=True, exist_ok=True)
    (sentinel / 'logs' / 'router' / 'shadow-2026-10-06.jsonl').write_text('{"sentinel": 1}\n')
    (sentinel / 'logs' / 'router' / 'outcomes' / 'turn-outcome-2026-10-06.jsonl').write_text('{"sentinel": 1}\n')
    (sentinel / 'logs' / 'router' / 'skipped-non-user-turn.json').write_text('{"sentinel": 1}\n')
    (sentinel / 'state' / 'session.json').write_text('{"sentinel": 1}\n')
    (sentinel / 'runtime' / 'router' / 'outcome.py').write_text('# sentinel\n')
    (sentinel / 'plugins' / 'jev-shadow-router' / '__init__.py').write_text('# sentinel\n')
    build_state_db(sentinel / 'db' / 'state.db', ['sentinel-session'])
    (sentinel / 'usage' / 'usage.db').write_text('{"sentinel": 1}\n')

    # ---- hostile parent environment ------------------------------------------------------
    hostile = {
        'HERMES_HOME': str(sentinel),
        'HERMES_STATE_DB': str(sentinel / 'db' / 'state.db'),
        'HERMES_USAGE_DB': str(sentinel / 'usage' / 'usage.db'),
        'JEV_ROUTER_ROOT': str(sentinel / 'runtime'),
        'JEV_LOG_DIR': str(sentinel / 'logs' / 'router'),
        'JEV_SKIP_COUNTER_PATH': str(sentinel / 'logs' / 'router' / 'skipped-non-user-turn.json'),
        'JEV_DEPLOYMENT_GENERATION': 'sentinel-generation',
        'JEV_AVAILABLE_ROUTES': 'sentinel_route',
    }
    sentinel_before = {name: fingerprint(p) for name, p in {
        'sentinel_logs': sentinel / 'logs', 'sentinel_state': sentinel / 'state',
        'sentinel_runtime': sentinel / 'runtime', 'sentinel_plugins': sentinel / 'plugins',
        'sentinel_db': sentinel / 'db', 'sentinel_usage': sentinel / 'usage'}.items()}

    os.environ.update(hostile)
    # effective fixture environment: everything this run may touch lives under tmp
    fixture_state = tmp / 'state'
    fixture_state.mkdir(parents=True, exist_ok=True)
    os.environ.update({'HERMES_HOME': str(fixture_home), 'JEV_LOG_DIR': str(fixture_logs),
                       'JEV_STATE_DIR': str(fixture_state),
                       'JEV_SKIP_COUNTER_PATH': str(fixture_logs / 'skipped-non-user-turn.json'),
                       'JEV_DEPLOYMENT_GENERATION': GENERATION_STAMP,
                       'JEV_AVAILABLE_ROUTES': 'deepseek_flash,mimo_pro',
                       'JEV_ALLOWED_PLATFORMS': 'feishu', 'ROUTER_MODE': 'shadow'})
    # Ambient values a running gateway exports must never reach an offline assertion.
    for _k in ('JEV_MIN_CONFIDENCE', 'JEV_MIN_MARGIN', 'JEV_TIMEOUT_SECONDS', 'JEV_MODEL'):
        os.environ.pop(_k, None)
    os.environ.pop('JEV_AUTO_APPROVED', None)
    (fixture_state / 'mode.json').write_text(json.dumps({'mode': 'shadow'}))

    sys.path.insert(0, str(ROOT))
    from router import config, outcome, skip_telemetry

    config._cache['at'] = 0
    config._cache['env'] = {}

    # The routing observation itself is stubbed: this suite must never emit a request to a
    # routing service. Only the observation *count* matters here — it proves the outcome
    # accumulator rides the same admission as the observation.
    from router import shadow as shadow_mod

    SUBMITS = []

    def _stub_submit(dossier, **kwargs):
        SUBMITS.append((kwargs.get('platform'), kwargs.get('turn_origin')))

    shadow_mod.submit = _stub_submit

    spec = importlib.util.spec_from_file_location('jev_plugin_under_test', ROOT / 'plugin' / '__init__.py')
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)

    chk('A0b telemetry path is inside the configured log directory',
        str(outcome.record_path()).startswith(str(fixture_logs)))
    chk('A0c generation stamp is the fixture one', config.deployment_generation() == GENERATION_STAMP)

    def records():
        return outcome.read_records(log_dir=fixture_logs)

    def by_corr(turn_id):
        corr = hashlib.sha256(turn_id.encode()).hexdigest()
        return [r for r in records() if r['turn_correlation'] == corr]

    def reset():
        outcome._reset_for_tests()

    # ---------------- A: usage arithmetic ----------------
    reset()
    plugin._on_pre_api_request(**pre('fx-A:1', 'fx-A'))
    plugin._on_post_api_request(**post('fx-A:1', 'fx-A', api_request_id='r1', input_tokens=1000,
                                       output_tokens=100, cache_read=800, cache_write=0,
                                       duration=1.0, finish_reason='tool_calls'))
    plugin._on_post_api_request(**post('fx-A:1', 'fx-A', api_request_id='r2', api_call_count=2,
                                       input_tokens=1200, output_tokens=150, cache_read=1000,
                                       cache_write=200, duration=1.5, finish_reason='stop'))
    plugin._on_post_llm_call(**final('fx-A:1', 'fx-A'))
    a = by_corr('fx-A:1')
    chk('A1 exactly one terminal record for the turn', len(a) == 1, str(len(a)))
    if a:
        r = a[0]
        chk('A2 api_request_count', r['api_request_count'] == 2, str(r['api_request_count']))
        chk('A3 api_input_tokens_sum', r['api_input_tokens_sum'] == 2200, str(r['api_input_tokens_sum']))
        chk('A4 api_output_tokens_sum', r['api_output_tokens_sum'] == 250, str(r['api_output_tokens_sum']))
        chk('A5 api_cache_read_tokens_sum', r['api_cache_read_tokens_sum'] == 1800,
            str(r['api_cache_read_tokens_sum']))
        chk('A6 api_cache_write_tokens_sum', r['api_cache_write_tokens_sum'] == 200,
            str(r['api_cache_write_tokens_sum']))
        chk('A7 api_input_tokens_max_request', r['api_input_tokens_max_request'] == 1200,
            str(r['api_input_tokens_max_request']))
        chk('A8 api_duration_ms_sum', r['api_duration_ms_sum'] == 2500, str(r['api_duration_ms_sum']))
        chk('A9 api_duration_ms_max', r['api_duration_ms_max'] == 1500, str(r['api_duration_ms_max']))
        chk('A10 prompt max is input+cache_read+cache_write of one request',
            r['api_prompt_tokens_max_request'] == 2400, str(r['api_prompt_tokens_max_request']))
        chk('A11 terminal status closed enum', r['terminal_status'] == 'completed')
        chk('A12 attribution quality', r['attribution_quality'] == 'EXACT_PROSPECTIVE')
        chk('A13 finish reason last is an enum token, not free text',
            r['finish_reason_last'] == 'stop', str(r['finish_reason_last']))
        chk('A14 schema version', r['schema_version'] == 'turn-outcome-v1')
        chk('A15 deployment generation', r['deployment_generation'] == GENERATION_STAMP)
        chk('A16 no content-bearing key exists in a record',
            not (set(r) & {'prompt', 'response', 'message', 'tool_name', 'tool_args', 'tool_result',
                           'memory', 'dossier', 'error', 'session_id', 'turn_id', 'task_id'}))
        chk('A17 summed input is not labelled a context size', 'context' not in json.dumps(r).lower())
        chk('A18 model attribution is content-free',
            r['actual_model_first'] == 'deepseek-flash' and r['model_switch_count'] == 0)
    chk('A19 the routing observation and the accumulator share one admission',
        SUBMITS == [('feishu', 'user')], json.dumps(SUBMITS))

    # ---------------- B: error, retry, then success ----------------
    reset()
    plugin._on_pre_api_request(**pre('fx-B:1', 'fx-B'))
    plugin._on_api_request_error(**err('fx-B:1', 'fx-B', api_request_id='r1'))
    plugin._on_post_api_request(**post('fx-B:1', 'fx-B', api_request_id='r2', api_call_count=2,
                                       input_tokens=500, output_tokens=50, cache_read=0, cache_write=0))
    plugin._on_post_llm_call(**final('fx-B:1', 'fx-B'))
    b = by_corr('fx-B:1')
    chk('B1 completed after a retry', len(b) == 1 and b[0]['terminal_status'] == 'completed')
    if b:
        chk('B2 error count preserved', b[0]['api_error_count'] == 1, str(b[0]['api_error_count']))
        chk('B3 success count preserved', b[0]['api_success_count'] == 1)
        chk('B4 retry observed', b[0]['retry_count_observed'] == 1)
        chk('B5 runtime error flag', b[0]['runtime_error_observed'] is True)
        chk('B6 request count spans both attempts', b[0]['api_request_count'] == 2)

    # ---------------- C: an error alone never fabricates a terminal ----------------
    reset()
    plugin._on_pre_api_request(**pre('fx-C:1', 'fx-C'))
    plugin._on_api_request_error(**err('fx-C:1', 'fx-C'))
    chk('C1 no terminal record after an error alone', by_corr('fx-C:1') == [])
    st = outcome.stats()
    chk('C2 the turn is still open, not silently completed',
        st['open_accumulators'] == 1 and st['finalized'] == 0, json.dumps(st))

    # ---------------- D: interruption ----------------
    reset()
    plugin._on_pre_api_request(**pre('fx-D:1', 'fx-D'))
    plugin._on_post_api_request(**post('fx-D:1', 'fx-D', input_tokens=300, output_tokens=30))
    plugin._on_agent_loop_stopped(**stopped('fx-D'))
    d = by_corr('fx-D:1')
    chk('D1 interruption finalizes an unambiguous turn',
        len(d) == 1 and d[0]['terminal_status'] == 'interrupted' and d[0]['interrupted_observed'])
    reset()
    plugin._on_pre_api_request(**pre('fx-D2:1', 'fx-D2'))
    plugin._on_pre_api_request(**pre('fx-D2:2', 'fx-D2', api_call_count=1))
    plugin._on_agent_loop_stopped(**stopped('fx-D2'))
    chk('D2 an ambiguous session key writes nothing',
        by_corr('fx-D2:1') == [] and by_corr('fx-D2:2') == [])
    chk('D3 ambiguous interruption is counted, not guessed',
        outcome.stats()['interrupt_unmatched'] >= 1)
    reset()
    plugin._on_agent_loop_stopped(**stopped('unknown-session'))
    chk('D4 an unknown session key writes nothing', outcome.stats()['interrupt_unmatched'] == 1)

    # ---------------- E: duplicate terminal ----------------
    reset()
    plugin._on_pre_api_request(**pre('fx-E:1', 'fx-E'))
    plugin._on_post_llm_call(**final('fx-E:1', 'fx-E'))
    plugin._on_post_llm_call(**final('fx-E:1', 'fx-E'))
    chk('E1 duplicate terminal hook yields one record', len(by_corr('fx-E:1')) == 1)
    chk('E2 the second terminal is counted as ignored',
        outcome.stats()['late_hook_ignored'] >= 1)

    # ---------------- F: duplicate response hook ----------------
    reset()
    plugin._on_pre_api_request(**pre('fx-F:1', 'fx-F'))
    for _ in range(3):
        plugin._on_post_api_request(**post('fx-F:1', 'fx-F', api_request_id='r1', input_tokens=100,
                                           output_tokens=10, cache_read=0, cache_write=0))
    plugin._on_post_llm_call(**final('fx-F:1', 'fx-F'))
    f = by_corr('fx-F:1')
    chk('F1 a duplicated request id is counted once',
        len(f) == 1 and f[0]['api_request_count'] == 1, str(f[0]['api_request_count'] if f else None))
    chk('F2 duplicate suppression is visible', outcome.stats()['duplicate_request'] == 2)

    # ---------------- G: late hook after finalization ----------------
    reset()
    plugin._on_pre_api_request(**pre('fx-G:1', 'fx-G'))
    plugin._on_post_llm_call(**final('fx-G:1', 'fx-G'))
    path = outcome.record_path()
    before = fingerprint(path)
    plugin._on_post_api_request(**post('fx-G:1', 'fx-G', api_request_id='late'))
    chk('G1 a late response hook does not rewrite the record', fingerprint(path) == before)
    chk('G2 the late hook is counted', outcome.stats()['late_hook_ignored'] >= 1)

    # ---------------- H: two simultaneous turns in one session ----------------
    reset()
    plugin._on_pre_api_request(**pre('fx-H:1', 'fx-H'))
    plugin._on_pre_api_request(**pre('fx-H:2', 'fx-H'))
    plugin._on_post_api_request(**post('fx-H:1', 'fx-H', api_request_id='h1r1', input_tokens=1000,
                                       output_tokens=100, cache_read=0, cache_write=0))
    plugin._on_post_api_request(**post('fx-H:2', 'fx-H', api_request_id='h2r1', input_tokens=700,
                                       output_tokens=70, cache_read=0, cache_write=0))
    plugin._on_post_api_request(**post('fx-H:1', 'fx-H', api_request_id='h1r2', api_call_count=2,
                                       input_tokens=200, output_tokens=20, cache_read=0, cache_write=0))
    plugin._on_post_llm_call(**final('fx-H:2', 'fx-H'))
    plugin._on_post_llm_call(**final('fx-H:1', 'fx-H'))
    h1, h2 = by_corr('fx-H:1'), by_corr('fx-H:2')
    chk('H1 both turns produced exactly one record', len(h1) == 1 and len(h2) == 1)
    if h1 and h2:
        chk('H2 first turn kept its own sums',
            h1[0]['api_input_tokens_sum'] == 1200 and h1[0]['api_request_count'] == 2,
            json.dumps([h1[0]['api_input_tokens_sum'], h1[0]['api_request_count']]))
        chk('H3 second turn kept its own sums',
            h2[0]['api_input_tokens_sum'] == 700 and h2[0]['api_request_count'] == 1)
        chk('H4 neither record absorbed the other turn (CROSS_TURN_CONTAMINATION = NO)',
            h1[0]['turn_correlation'] != h2[0]['turn_correlation']
            and max(h1[0]['api_request_count'], h2[0]['api_request_count']) == 2
            and {h1[0]['api_input_tokens_sum'], h2[0]['api_input_tokens_sum']} == {1200, 700})

    # ---------------- I: two simultaneous sessions ----------------
    reset()
    plugin._on_pre_api_request(**pre('fx-I:1', 'fx-I1', model='deepseek-flash'))
    plugin._on_pre_api_request(**pre('fx-I:2', 'fx-I2', model='mimo-pro', platform='feishu'))
    plugin._on_post_api_request(**post('fx-I:1', 'fx-I1', api_request_id='i1r1', input_tokens=11,
                                       output_tokens=1, cache_read=0, cache_write=0))
    plugin._on_post_api_request(**post('fx-I:2', 'fx-I2', api_request_id='i2r1', input_tokens=22,
                                       output_tokens=2, cache_read=0, cache_write=0,
                                       model='mimo-pro'))
    plugin._on_agent_loop_stopped(**stopped('fx-I2'))
    plugin._on_post_llm_call(**final('fx-I:1', 'fx-I1'))
    i1, i2 = by_corr('fx-I:1'), by_corr('fx-I:2')
    chk('I1 each session finalized independently',
        len(i1) == 1 and len(i2) == 1
        and i1[0]['terminal_status'] == 'completed' and i2[0]['terminal_status'] == 'interrupted',
        json.dumps([i1 and i1[0]['terminal_status'], i2 and i2[0]['terminal_status']]))
    if i1 and i2:
        chk('I2 session-local model attribution',
            i1[0]['actual_model_last'] == 'deepseek-flash' and i2[0]['actual_model_last'] == 'mimo-pro')
        chk('I3 session-local usage', i1[0]['api_input_tokens_sum'] == 11 and i2[0]['api_input_tokens_sum'] == 22)

    # ---------------- J: content blindness ----------------
    reset()
    plugin._on_pre_api_request(**pre('fx-J:1', 'fx-J'))
    plugin._on_post_api_request(**post('fx-J:1', 'fx-J', api_request_id='r1'))
    plugin._on_api_request_error(**err('fx-J:1', 'fx-J', api_request_id='r2'))
    plugin._on_post_api_request(**post('fx-J:1', 'fx-J', api_request_id='r3'))
    plugin._on_post_llm_call(**final('fx-J:1', 'fx-J'))
    file_bytes = outcome.record_path().read_text()
    j = by_corr('fx-J:1')
    chk('J1 the turn produced a record', len(j) == 1)
    chk('J2 no sentinel reaches the telemetry file',
        not any(s in file_bytes for s in ALL_SENTINELS),
        [s for s in ALL_SENTINELS if s in file_bytes])
    chk('J3 correlation is sha256 of the structural turn id only',
        j and j[0]['turn_correlation'] == hashlib.sha256(b'fx-J:1').hexdigest())
    chk('J4 no content value was hashed into the record',
        not any(hashlib.sha256(s.encode()).hexdigest() in file_bytes for s in ALL_SENTINELS))
    chk('J5 allowlists contain no content key',
        not ({'user_message', 'conversation_history', 'request_messages', 'system_prompt',
              'assistant_response', 'response', 'assistant_message', 'error', 'request'}
             & (outcome._PRE_KEYS | outcome._POST_KEYS | outcome._ERROR_KEYS | outcome._FINAL_KEYS
                | outcome._STOPPED_KEYS)))
    chk('J6 stats carry no content', not any(s in json.dumps(outcome.stats()) for s in ALL_SENTINELS))

    # ---------------- K: boundary reuse and unchanged counters ----------------
    reset()
    counter_before = skip_telemetry.read()
    submits_before = len(SUBMITS)
    plugin._on_pre_api_request(**pre('fx-K:1', 'fx-K', platform='cli'))
    plugin._on_pre_api_request(**pre('fx-K:2', 'fx-K', origin='background_review'))
    plugin._on_pre_api_request(**pre('fx-K:3', 'fx-K', origin=None))
    plugin._on_pre_api_request(**pre('fx-K:4', 'fx-K', platform=None))
    chk('K1 a non-admitted turn gets no accumulator', outcome.stats()['open_accumulators'] == 0,
        json.dumps(outcome.stats()))
    chk('K2 no outcome record for a non-admitted turn', records() == [] or
        not any(r['turn_correlation'] in {hashlib.sha256(f'fx-K:{i}'.encode()).hexdigest()
                                          for i in range(1, 5)} for r in records()))
    counter_after = skip_telemetry.read()
    chk('K3 rejection counters still count every rejected turn',
        json.dumps(counter_after) != json.dumps(counter_before)
        and sum(c['count'] for d in counter_after.values() for c in d['counters'].values())
        >= sum(c['count'] for d in counter_before.values() for c in d['counters'].values()) + 4)
    chk('K4 a rejected turn is counted as an admission failure, not as telemetry',
        outcome.stats()['started'] == 0)
    chk('K5 no routing observation was attempted either (same boundary)',
        not any(p == 'cli' or o == 'background_review' for p, o in SUBMITS))
    chk('K6 no observation and no accumulator for a rejected turn',
        outcome.stats()['started'] == 0 and len(SUBMITS) == submits_before)

    # ---------------- L: bounded memory ----------------
    reset()
    for i in range(outcome._MAX_OPEN + 6):
        plugin._on_pre_api_request(**pre(f'fx-L:{i}', 'fx-L'))
    st = outcome.stats()
    chk('L1 open accumulators stay bounded', st['open_accumulators'] <= outcome._MAX_OPEN,
        str(st['open_accumulators']))
    chk('L2 eviction is counted', st['evicted_unfinished'] == 6, str(st['evicted_unfinished']))
    chk('L3 eviction never fabricates a terminal record',
        st['finalized'] == 0 and not any(r['turn_correlation'] == hashlib.sha256(b'fx-L:0').hexdigest()
                                         for r in records()))

    # ---------------- M: append-only, permissions, no truncation ----------------
    reset()
    plugin._on_pre_api_request(**pre('fx-M:1', 'fx-M'))
    plugin._on_post_llm_call(**final('fx-M:1', 'fx-M'))
    path = outcome.record_path()
    prefix = path.read_bytes()
    lines_before = len(prefix.decode().strip().splitlines())
    time.sleep(0.01)
    plugin._on_pre_api_request(**pre('fx-M:2', 'fx-M'))
    plugin._on_post_llm_call(**final('fx-M:2', 'fx-M'))
    after = path.read_bytes()
    chk('M1 the file only grows', len(after) > len(prefix) and after.startswith(prefix))
    chk('M2 exactly one line appended per terminal record',
        len(after.decode().strip().splitlines()) == lines_before + 1,
        f'{lines_before} -> {len(after.decode().strip().splitlines())}')
    # The code requests 0o600 for the file and 0o700 for the directory; the assertion is the
    # property that matters, because a filesystem may report different owner bits (this
    # container reports 0o700 for a freshly created 0o600 file).
    chk('M3 the record file is not group- or world-accessible',
        (path.stat().st_mode & 0o077) == 0, oct(path.stat().st_mode & 0o777))
    chk('M4 the outcome directory is not group- or world-accessible',
        (path.parent.stat().st_mode & 0o077) == 0, oct(path.parent.stat().st_mode & 0o777))
    chk('M5 the writer requests restrictive modes',
        '0o600' in (ROOT / 'router' / 'outcome.py').read_text().replace('0o600', '0o600')
        and 'mode=0o700' in (ROOT / 'router' / 'outcome.py').read_text())

    # ---------------- N: failure policy ----------------
    reset()
    plugin._on_pre_api_request(**pre('fx-N:1', 'fx-N'))
    real_open = os.open
    try:
        os.open = lambda *a, **k: (_ for _ in ()).throw(OSError('disk full'))
        result = outcome.finalize(final('fx-N:1', 'fx-N'), 'completed')
        raised = False
    except Exception:
        raised = True
        result = None
    finally:
        os.open = real_open
    chk('N1 a write failure never raises out of the module', raised is False and result is False,
        f'raised={raised} result={result}')
    chk('N2 the failure is counted for analytics',
        outcome.stats()['write_failed'] == 1 and outcome.stats()['finalized'] == 0)
    reset()
    plugin._on_pre_api_request(**pre('fx-N:2', 'fx-N'))
    real_write = outcome.observe_response
    try:
        outcome.observe_response = lambda kw: (_ for _ in ()).throw(RuntimeError('boom'))
        plugin._on_post_api_request(**post('fx-N:2', 'fx-N'))
        raised = False
    except Exception:
        raised = True
    finally:
        outcome.observe_response = real_write
    chk('N3 a handler failure never propagates to the agent turn', raised is False)
    chk('N4 handlers always return None',
        plugin._on_post_api_request(**post('fx-N:2', 'fx-N')) is None
        and plugin._on_post_llm_call(**final('fx-N:2', 'fx-N')) is None
        and plugin._on_agent_loop_stopped(**stopped('nope')) is None)
    chk('N5 missing terminal is not success by construction',
        'MISSING_TERMINAL_IS_SUCCESS' in (ROOT / 'router' / 'outcome.py').read_text())

    # ---------------- O: analyzer ----------------
    agg_dir = tmp / 'aggregate-outcomes'
    agg_dir.mkdir()
    shadow_dir = tmp / 'aggregate-shadow'
    shadow_dir.mkdir()
    state_db = tmp / 'aggregate-state.db'
    gen_sessions = ['fx-A', 'fx-B', 'fx-C']
    build_state_db(state_db, gen_sessions)
    sh_sha = GEN
    lines = []
    for turn_id, conf, would in (('fx-A:1', 0.58, 'mimo_pro'), ('fx-B:1', 0.90, 'deepseek_flash'),
                                 ('fx-C:1', 0.40, 'mimo_pro')):
        lines.append(json.dumps({'turn_id': turn_id, 'platform': 'feishu', 'turn_origin': 'user',
                                 'route': 'deepseek_flash', 'confidence': conf, 'success': True,
                                 'would_execute': would, 'mode': 'shadow',
                                 'actual_model': 'deepseek-flash', 'timestamp': '2026-10-06T10:00:00Z',
                                 'latency_ms': 1000, 'jev_model': 'jev-test',
                                 'deployment_generation': sh_sha}))
    (shadow_dir / 'shadow-2026-10-06.jsonl').write_text('\n'.join(lines) + '\n')

    def ledger(digest, status='completed', **extra):
        rec = {'schema_version': 'turn-outcome-v1', 'deployment_generation': GEN,
               'platform': 'feishu', 'turn_origin': 'user', 'turn_correlation': digest,
               'actual_model_first': 'deepseek-flash', 'actual_model_last': 'deepseek-flash',
               'model_switch_count': 0, 'terminal_status': status, 'duration_ms': 1234,
               'api_request_count': 2, 'api_success_count': 2, 'api_error_count': 0,
               'retry_count_observed': 0, 'api_input_tokens_sum': 2200, 'api_output_tokens_sum': 250,
               'api_cache_read_tokens_sum': 1800, 'api_cache_write_tokens_sum': 200,
               'api_input_tokens_max_request': 1200, 'api_output_tokens_max_request': 150,
               'api_cache_read_tokens_max_request': 1000, 'api_cache_write_tokens_max_request': 200,
               'api_prompt_tokens_max_request': 2400, 'api_duration_ms_sum': 2500,
               'api_duration_ms_max': 1500, 'runtime_error_observed': False,
               'interrupted_observed': status == 'interrupted',
               'attribution_quality': 'EXACT_PROSPECTIVE'}
        rec.update(extra)
        return rec

    digest_a = hashlib.sha256(b'fx-A:1').hexdigest()
    digest_c = hashlib.sha256(b'fx-C:1').hexdigest()
    poisoned = ledger(hashlib.sha256(b'fx-P:1').hexdigest(), prompt=PROMPT_SENTINEL,
                      assistant_response=RESPONSE_SENTINEL)
    (agg_dir / 'turn-outcome-2026-10-06.jsonl').write_text('\n'.join([
        json.dumps(ledger(digest_a)), json.dumps(ledger(digest_c, status='interrupted')),
        json.dumps(poisoned)]) + '\n')

    env = dict(os.environ, HERMES_HOME=str(fixture_home), JEV_LOG_DIR=str(fixture_logs),
               HERMES_STATE_DB=str(state_db))
    proc = subprocess.run([sys.executable, str(TOOL), '--generation', GEN, '--log-dir', str(shadow_dir),
                           '--state-db', str(state_db), '--prospective-telemetry', str(agg_dir)],
                          capture_output=True, text=True, env=env)
    chk('O0 analyzer runs', proc.returncode == 0, proc.stderr[-300:])
    if proc.returncode == 0:
        out = json.loads(proc.stdout)
        p = out.get('PROSPECTIVE_OUTCOME_TELEMETRY', {})
        g = (p.get('PER_GENERATION') or {}).get(GEN, {})
        chk('O1 terminal coverage uses shadow turns as the denominator',
            g.get('ADMITTED_SHADOW_TURNS') == 3, json.dumps(g.get('ADMITTED_SHADOW_TURNS')))
        chk('O2 terminal outcomes counted', g.get('TERMINAL_OUTCOMES') == 2, json.dumps(g))
        chk('O3 the gap stays visible', g.get('MISSING_TERMINAL_OUTCOMES') == 1, json.dumps(g))
        chk('O4 missing terminal is never success', p.get('MISSING_TERMINAL_IS_SUCCESS') == 'NO'
            and g.get('MISSING_TERMINAL_IS_SUCCESS') == 'NO')
        chk('O5 status distribution is closed-enum based',
            g.get('terminal_status_distribution') == {'completed': 1, 'interrupted': 1},
            json.dumps(g.get('terminal_status_distribution')))
        chk('O6 request-count and usage distributions are present',
            g.get('api_request_count', {}).get('p50') == 2
            and g.get('api_input_tokens_sum', {}).get('max') == 2200)
        chk('O7 a record with an unexpected field is refused',
            p.get('REJECTED_UNEXPECTED_FIELD_RECORDS') == 1, json.dumps(p.get('REJECTED_UNEXPECTED_FIELD_RECORDS')))
        chk('O8 no sentinel reaches the aggregate',
            not any(s in proc.stdout for s in ALL_SENTINELS))
        chk('O9 no raw correlation digest reaches the aggregate',
            digest_a not in proc.stdout and digest_c not in proc.stdout)
        chk('O10 the historical audit is unchanged alongside it',
            'HISTORICAL_OUTCOME_JOIN_COVERAGE' in out and 'PER_TURN_EXACTNESS' in out
            and out['PER_TURN_EXACTNESS']['INPUT_TOKENS_PER_TURN_EXACT'] == 'NO')
        chk('O11 prospective availability flags are explicit',
            out.get('PER_REQUEST_USAGE_AVAILABLE') == 'YES'
            and out.get('PER_TURN_CONTEXT_SNAPSHOT_AVAILABLE') == 'NO'
            and out.get('PER_TURN_CACHE_HIT_BOOLEAN_AVAILABLE') == 'NO'
            and out.get('CACHE_SWITCH_COUNTERFACTUAL_COST') == 'NO')
        chk('O12 correlation is never presented as causation',
            p.get('CORRELATION_NOT_CAUSATION') == 'YES')
    empty = tmp / 'empty-outcomes'
    empty.mkdir()
    proc2 = subprocess.run([sys.executable, str(TOOL), '--generation', GEN, '--log-dir', str(shadow_dir),
                            '--state-db', str(state_db), '--prospective-telemetry', str(empty)],
                           capture_output=True, text=True, env=env)
    # Careful: the *key* must be absent. A substring test would match the unrelated
    # PROSPECTIVE_OUTCOME_TELEMETRY_REQUIRED flag.
    proc2_json = json.loads(proc2.stdout) if proc2.returncode == 0 else {}
    chk('O13 without telemetry the prospective block is absent',
        proc2.returncode == 0 and 'PROSPECTIVE_OUTCOME_TELEMETRY' not in proc2_json
        and proc2_json.get('PROSPECTIVE_OUTCOME_TELEMETRY_REQUIRED') == 'YES',
        f'rc={proc2.returncode} stderr={proc2.stderr[-200:]}')

    # ---------------- P: sentinel isolation ----------------
    sentinel_after = {name: fingerprint(p) for name, p in {
        'sentinel_logs': sentinel / 'logs', 'sentinel_state': sentinel / 'state',
        'sentinel_runtime': sentinel / 'runtime', 'sentinel_plugins': sentinel / 'plugins',
        'sentinel_db': sentinel / 'db', 'sentinel_usage': sentinel / 'usage'}.items()}
    for name in sentinel_before:
        chk(f'P.{name} sentinel path byte-identical', sentinel_before[name] == sentinel_after[name],
            f'{name} was touched by an offline run')
    chk('P1 all telemetry landed in the temp tree',
        str(outcome.record_path()).startswith(str(tmp)))
    chk('P2 the suite never referenced the sentinel telemetry path',
        str(sentinel / 'logs') != str(config.LOG_DIR))
    chk('P3 no production path is reachable from the suite environment',
        '/opt/data/logs' not in str(config.LOG_DIR) and '/opt/data/state.db' != str(config.ENV_PATH))

    report()
    return 1 if FAILS else 0


def report():
    print(f'{"=" * 74}\nprospective per-turn outcome telemetry — results\n{"=" * 74}')
    for name, ok, extra in R:
        print(f'  [{"PASS" if ok else "FAIL"}] {name}' + (f'  <- {extra}' if extra and not ok else ''))
    print(f'\n{len(R) - len(FAILS)}/{len(R)} passed')
    if FAILS:
        print('FAILED: ' + ', '.join(FAILS))


if __name__ == '__main__':
    raise SystemExit(main())
