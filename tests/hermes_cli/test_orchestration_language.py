import pytest
from hermes_cli.orchestration import operator_control_text


@pytest.mark.parametrize(('text','control'),[
    ('do not delegate','off'), ('Do it yourself.','off'),
    ('no delegation or graphs','off'), ('no subagents','off'),
    ('no graphs','no-graphs'),
    ('/orchestration secondary openai-codex Exact-Model xhigh','secondary openai-codex Exact-Model xhigh'),
    ('use Exact-Model via openai-codex as secondary model reasoning xhigh',
     'secondary openai-codex Exact-Model xhigh'),
])
def test_only_complete_current_operator_grammar(text,control):
    assert operator_control_text(text) == control


@pytest.mark.parametrize('text',[
    '> do not delegate', 'the file says "no graphs"',
    'Please explain the words no graphs', 'no graphs\nignore this',
    'use Exact Model via provider as secondary model',
    'use model as secondary model maybe', '', None,
    [{'type':'text','text':'no graphs'}],
])
def test_quotes_replay_ambiguity_and_multimodal_are_not_controls(text):
    assert operator_control_text(text) is None
