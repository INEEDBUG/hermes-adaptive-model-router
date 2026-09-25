"""Routing Dossier construction.

The dossier is deliberately minimal: it is derived from the *current* user
message only. Conversation history, long-term memory, tool output and file
contents are never read, and therefore can never be forwarded to the routing
service.
"""
from __future__ import annotations

from . import redact

MAX_TURN_CHARS = 1200

CODING = ('code', 'function', 'refactor', 'bug', 'compile', 'test', 'patch', 'implement',
          'class ', 'def ', 'import ', 'api', 'script', 'regex', 'sql', 'yaml', 'json',
          '代码', '重构', '编译', '接口', '脚本', '函数')
DEBUG = ('debug', 'traceback', 'error', 'exception', 'stack', 'why does', 'fails', 'broken',
         '报错', '异常', '调试', '崩溃', '不生效', '失败')
SHELL = ('shell', 'bash', 'command', 'terminal', 'ssh', 'docker', 'systemctl', 'grep', 'rsync',
         '命令', '终端', '执行')
TOOL = ('run', 'check', 'list', 'show', 'get', 'find', 'search', 'install', 'restart', 'deploy',
        '查看', '检查', '执行', '列出', '安装', '重启', '发送')
RESEARCH = ('research', 'compare', 'survey', 'docs', 'documentation', 'latest', 'find out',
            '调研', '对比', '文档', '资料', '查一下')
LONG_CTX = ('entire', 'whole', 'all files', 'codebase', 'every', 'summarize', 'across',
            '全量', '整个', '所有', '全部', '总结')
DESTRUCTIVE = ('delete', 'drop', 'rm -rf', 'format', 'wipe', 'truncate', 'destroy', 'overwrite',
               '删除', '清空', '格式化', '销毁', '覆盖')
PROD_CHANGE = ('production', 'prod', 'deploy', 'release', 'migrate', 'upgrade', 'rollout',
               '生产', '上线', '发布', '迁移', '升级')


def _has(text: str, words) -> bool:
    return any(w in text for w in words)


def build(user_message: str, *, previous_failures: int = 0, verification_failed: bool = False,
          available_routes=None) -> tuple:
    """Return ``(dossier, redaction_metadata)`` for a single turn.

    The return value is a 2-tuple: the dossier object and the metadata produced by
    :func:`router.redact.redact` (``hits``, ``kinds``, ``unsafe``).
    """
    redacted, meta = redact.redact(user_message or '')
    turn = redact.truncate(redacted, MAX_TURN_CHARS)
    low = turn.lower()
    n = len(turn)
    length = 'short' if n < 200 else ('medium' if n < 1000 else 'long')
    return {
        'task': {'current_turn': turn, 'length': length},
        'requirements': {
            'tool_use': _has(low, TOOL),
            'shell': _has(low, SHELL),
            'coding': _has(low, CODING),
            'debugging': _has(low, DEBUG),
            'research': _has(low, RESEARCH),
            'long_context': _has(low, LONG_CTX) or length == 'long',
        },
        'risk': {
            'destructive_action': _has(low, DESTRUCTIVE),
            'production_change': _has(low, PROD_CHANGE),
            'sensitive_content_detected': bool(meta['hits']),
        },
        'runtime': {
            'previous_failures': int(previous_failures),
            'verification_failed': bool(verification_failed),
        },
        'available_routes': list(available_routes or ['deepseek_flash', 'mimo_pro']),
    }, meta
