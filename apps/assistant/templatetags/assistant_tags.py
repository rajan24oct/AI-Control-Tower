from django import template

from apps.assistant.answers import AGENT_NAME, FRAMEWORK_URL, GUIDED

register = template.Library()


@register.simple_tag
def guided_questions():
    """The agent's guided questions, by topic: [(topic, [question, ...]), ...]."""
    return GUIDED


@register.simple_tag
def agent_name():
    return AGENT_NAME


@register.simple_tag
def framework_url():
    return FRAMEWORK_URL
