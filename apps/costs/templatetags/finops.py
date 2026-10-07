from django import template

register = template.Library()

#: How evidence keys read on the page.
EVIDENCE_LABELS = {
    "runs": "Runs",
    "input_tokens": "Input tokens",
    "output_tokens": "Output tokens",
    "cost_ratio": "Cost of the alternative (fraction)",
    "movable_share": "Traffic assumed movable",
    "same_family": "Same model family",
    "same_provider": "Same provider",
    "cached_discount": "Cached-input discount",
    "discount_from_price_list": "Discount from the price list",
    "cacheable_share": "Input assumed cacheable",
    "avg_output_tokens": "Average output tokens",
    "estate_median": "Estate median output tokens",
    "share_of_spend": "Share of runtime spend (%)",
}


@register.filter
def evidence_label(key: str) -> str:
    return EVIDENCE_LABELS.get(key, str(key).replace("_", " ").capitalize())
