"""Template context for the in-page help panel."""

from __future__ import annotations

from django.urls import NoReverseMatch, resolve, reverse

from apps.core.help import help_for
from apps.organizations.modules import is_enabled, module_for_app


def module_help(request):
    """The help for the page being rendered, with its links and API root resolved."""
    user = getattr(request, "user", None)
    match = getattr(request, "resolver_match", None)
    # AJAX region swaps re-render the page but only keep the main region, so
    # the panel is built for full page loads only.
    if (not user or not user.is_authenticated or match is None
            or request.headers.get("X-Requested-With") == "XMLHttpRequest"):
        return {}
    organization = getattr(request, "organization", None)
    module = module_for_app(match.app_name)
    if module and not is_enabled(organization, module.key):
        return {}
    entry = help_for(match.app_name, match.url_name)
    if not entry:
        return {}

    api = request.build_absolute_uri("/api/v1").rstrip("/")
    related = []
    for label, name in entry.get("related", []):
        try:
            url = reverse(name)
        except NoReverseMatch:
            continue
        # A link into a module the organization has switched off goes nowhere useful.
        linked = module_for_app(resolve(url).app_name)
        if linked and not is_enabled(organization, linked.key):
            continue
        related.append({"label": label, "url": url})
    return {
        "module_help": {
            **entry,
            "summary": entry["summary"].replace("{api}", api),
            "examples": [
                {**example,
                 "text": example["text"].replace("{api}", api),
                 "code": example["code"].replace("{api}", api)}
                for example in entry.get("examples", [])
            ],
            "related": related,
        }
    }
