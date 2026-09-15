"""Form building blocks for the in-app management pages.

Registry records keep lists and small objects in JSON fields. Nobody should
have to type JSON to say "these three tools", so each shape gets a field that
reads and writes its natural form: names separated by commas, a set of
checkboxes, or -- for genuinely free-form adapter settings -- a JSON object.
"""

from __future__ import annotations

import json

from django import forms
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.utils.text import slugify

from apps.organizations.models import OrganizationMembership


def members(organization):
    """People who belong to the organization, for owner pickers."""
    return get_user_model().objects.filter(
        pk__in=OrganizationMembership.objects.filter(organization=organization).values("user_id")
    ).order_by("first_name", "last_name", "email")


def person_label(user) -> str:
    return user.get_full_name() or user.email


class ListField(forms.CharField):
    """A list of names, entered one per line or separated by commas."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("required", False)
        super().__init__(*args, **kwargs)

    def prepare_value(self, value):
        if isinstance(value, (list, tuple)):
            return ", ".join(str(item) for item in value)
        return value

    def to_python(self, value):
        if isinstance(value, (list, tuple)):
            items = value
        else:
            items = (super().to_python(value) or "").replace("\n", ",").split(",")
        cleaned: list[str] = []
        for item in items:
            item = str(item).strip()
            if item and item not in cleaned:
                cleaned.append(item)
        return cleaned


class IntegerListField(ListField):
    """Whole numbers separated by commas, e.g. budget alert percentages."""

    def __init__(self, *args, min_value: int | None = None, max_value: int | None = None, **kwargs):
        self.min_value, self.max_value = min_value, max_value
        super().__init__(*args, **kwargs)

    def to_python(self, value):
        numbers = []
        for item in super().to_python(value):
            try:
                number = int(item)
            except (TypeError, ValueError):
                raise ValidationError(f"“{item}” is not a whole number.") from None
            if self.min_value is not None and number < self.min_value:
                raise ValidationError(f"{number} is below the minimum of {self.min_value}.")
            if self.max_value is not None and number > self.max_value:
                raise ValidationError(f"{number} is above the maximum of {self.max_value}.")
            numbers.append(number)
        return sorted(set(numbers))


class ChoiceListField(forms.TypedMultipleChoiceField):
    """A list drawn from fixed choices, shown as checkboxes."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("required", False)
        kwargs.setdefault("widget", forms.CheckboxSelectMultiple)
        super().__init__(*args, **kwargs)

    def include(self, values) -> None:
        """Keep values a record already holds selectable, even off the standard list."""
        known = {str(value) for value, _ in self.choices}
        extra = [(value, value) for value in values or [] if str(value) not in known]
        if extra:
            self.choices = [*self.choices, *extra]


class JSONObjectField(forms.JSONField):
    """A small JSON object, for free-form, non-secret adapter settings."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("required", False)
        kwargs.setdefault("widget", forms.Textarea(attrs={"rows": 4, "class": "font-monospace"}))
        super().__init__(*args, **kwargs)

    def to_python(self, value):
        value = super().to_python(value)
        if value in (None, ""):
            return {}
        if not isinstance(value, dict):
            raise ValidationError('Enter a JSON object, for example {"timeout": 30}.')
        return value

    def prepare_value(self, value):
        if isinstance(value, str):
            return value
        if not value:
            return ""
        return json.dumps(value, indent=2, sort_keys=True)


class StyledFormMixin:
    """Bootstrap classes on every widget, so a template can render fields as they are."""

    def apply_styles(self) -> None:
        for field in self.fields.values():
            widget = field.widget
            if isinstance(widget, (forms.CheckboxSelectMultiple, forms.RadioSelect, forms.CheckboxInput)):
                css = "form-check-input"
            elif isinstance(widget, (forms.Select, forms.SelectMultiple)):
                css = "form-select"
            else:
                css = "form-control"
            classes = widget.attrs.get("class", "").split()
            if css not in classes:
                widget.attrs["class"] = " ".join([*classes, css])
            if isinstance(widget, forms.Textarea) and widget.attrs.get("rows") in (None, "10", 10):
                widget.attrs["rows"] = 3
            if isinstance(field, forms.DateField) and not isinstance(field, forms.DateTimeField):
                widget.input_type = "date"
                widget.format = "%Y-%m-%d"


class SectionedFormMixin:
    """Lays a form out in titled sections for the generic `manage/form.html`."""

    #: Page sections: (title, description, [field names]) or with a fourth
    #: item, a dict of options: {"tag": "IRGF S3"}.
    sections: tuple = ()
    #: Layout: fields shown at half or a third of the width.
    compact_fields: tuple[str, ...] = ()
    narrow_fields: tuple[str, ...] = ()
    #: Fields shown only while another holds a value: {"field": "other:value1,value2"}.
    show_when: dict[str, str] = {}

    def section_list(self) -> list[dict]:
        """The form laid out in its sections, for the generic template."""

        def item(name):
            if name in self.compact_fields:
                column = "col-12 col-md-6"
            elif name in self.narrow_fields:
                column = "col-12 col-md-4"
            else:
                column = "col-12"
            return {"field": self[name], "col": column, "show_when": self.show_when.get(name, "")}

        placed: set[str] = set()
        result = []
        for section in self.sections:
            title, description, names = section[:3]
            options = section[3] if len(section) > 3 else {}
            present = [name for name in names if name in self.fields]
            placed.update(names)
            if present:
                result.append({
                    "title": title, "description": description, "tag": options.get("tag", ""),
                    "fields": [item(name) for name in present],
                })
        rest = [name for name in self.fields if name not in placed]
        if rest:
            result.append({"title": "More" if result else "", "description": "", "tag": "",
                           "fields": [item(name) for name in rest]})
        return result


class OrganizationModelForm(StyledFormMixin, SectionedFormMixin, forms.ModelForm):
    """A model form for one organization's records.

    Every choice of another record is limited to the organization, the new
    record is created in it, and values that must be unique within an
    organization are checked here: the database constraint includes the
    organization, which a model form leaves out of its own checks.
    """

    #: Fields whose value must be unique within the organization.
    unique_in_organization: tuple[str, ...] = ()
    #: The field a blank slug is derived from, where the model derives one.
    slug_source: str = ""

    def __init__(self, *args, organization=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.organization = organization
        if organization is not None and self.creating and hasattr(self.instance, "organization_id"):
            if not self.instance.organization_id:
                self.instance.organization = organization
        self.scope_choices()
        self.apply_styles()

    @property
    def creating(self) -> bool:
        # Not `instance.pk`: UUID primary keys are assigned before the first save.
        return self.instance._state.adding

    def scope_choices(self) -> None:
        user_model = get_user_model()
        for field in self.fields.values():
            queryset = getattr(field, "queryset", None)
            if queryset is None:
                continue
            if queryset.model is user_model:
                field.queryset = members(self.organization)
                field.label_from_instance = person_label
            elif any(f.name == "organization" for f in queryset.model._meta.fields):
                field.queryset = queryset.filter(organization=self.organization)

    def clean(self):
        cleaned = super().clean()
        model = self._meta.model
        noun = model._meta.verbose_name
        for name in self.unique_in_organization:
            value = cleaned.get(name)
            if value in (None, ""):
                continue
            lookup = {f"{name}__iexact": value} if isinstance(value, str) else {name: value}
            clash = model.objects.filter(organization=self.organization, **lookup)
            if not self.creating:
                clash = clash.exclude(pk=self.instance.pk)
            if clash.exists():
                self.add_error(name, f"Another {noun} in this organization already uses this {self.fields[name].label.lower()}.")
        if self.slug_source and not getattr(self.instance, "slug", "") and cleaned.get(self.slug_source):
            slug = slugify(cleaned[self.slug_source])[:200]
            clash = model.objects.filter(organization=self.organization, slug=slug)
            if not self.creating:
                clash = clash.exclude(pk=self.instance.pk)
            if slug and clash.exists():
                self.add_error(self.slug_source, f"Another {noun} in this organization already has an identical name once spaces and punctuation are ignored.")
        return cleaned
