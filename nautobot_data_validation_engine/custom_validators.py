"""
This is the meat of this app.

Here we dynamically generate a PluginCustomValidator class
for each model currently registered in the extras_features
query registry 'custom_validators'.

A common clean method for all these classes looks for any
validation rules that have been defined for the given model.
"""

import inspect
import logging
import pkgutil
import re
import sys
from typing import Optional

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.template.defaultfilters import pluralize
from django.utils import timezone
from nautobot.core.utils.data import render_jinja2
from nautobot.extras.datasources import ensure_git_repository
from nautobot.extras.models import GitRepository
from nautobot.extras.plugins import CustomValidator, PluginCustomValidator
from nautobot.extras.registry import registry

from nautobot_data_validation_engine.models import (
    DataCompliance,
    MinMaxValidationRule,
    RegularExpressionValidationRule,
    RequiredValidationRule,
    UniqueValidationRule,
    validate_regex,
)

LOGGER = logging.getLogger(__name__)


class BaseValidator(PluginCustomValidator):
    """Base PluginCustomValidator class that implements the core logic for enforcing validation rules defined in this app."""

    model = None

    def clean(self, exclude_disabled_rules=True):  # pylint: disable=too-many-branches
        """The clean method executes the actual rule enforcement logic for each model."""
        obj = self.context["object"]

        _f = [True] if exclude_disabled_rules else [True, False]

        # Regex rules
        for rule in RegularExpressionValidationRule.objects.get_for_model(self.model).filter(enabled__in=_f):
            field_value = getattr(obj, rule.field)

            if field_value is None:
                # Coerce to a string for regex validation
                field_value = ""

            if rule.context_processing:
                # Render the regular_expression as a jinja2 string and ensure it is valid
                try:
                    regular_expression = render_jinja2(rule.regular_expression, self.context)
                    validate_regex(regular_expression)
                # TODO: Switch to a less broad exception.
                except Exception:  # pylint: disable=broad-exception-caught
                    LOGGER.exception(
                        "There was an error rendering the regular expression in the data validation rule '%s' and a ValidationError was raised!",
                        rule,
                    )
                    self.validation_error(
                        {
                            rule.field: f"There was an error rendering the regular expression in the data validation rule '{rule}'. "
                            "Either fix the validation rule or disable it in order to save this data."
                        }
                    )

            else:
                regular_expression = rule.regular_expression

            if not re.match(regular_expression, field_value):
                self.validation_error(
                    {rule.field: rule.error_message or f"Value does not conform to regex: {regular_expression}"}
                )

        # Min/Max rules
        for rule in MinMaxValidationRule.objects.get_for_model(self.model).filter(enabled__in=_f):
            field_value = getattr(obj, rule.field)

            if field_value is None:
                self.validation_error(
                    {
                        rule.field: rule.error_message
                        or f"Value does not conform to mix/max validation: min {rule.min}, max {rule.max}"
                    }
                )

            elif not isinstance(field_value, (int, float)):
                self.validation_error(
                    {
                        rule.field: f"Unable to validate against min/max rule {rule} because the field value is not numeric."
                    }
                )

            elif rule.min is not None and field_value is not None and field_value < rule.min:
                self.validation_error(
                    {rule.field: rule.error_message or f"Value is less than minimum value: {rule.min}"}
                )

            elif rule.max is not None and field_value is not None and field_value > rule.max:
                self.validation_error(
                    {rule.field: rule.error_message or f"Value is more than maximum value: {rule.max}"}
                )

        # Required rules
        for rule in RequiredValidationRule.objects.get_for_model(self.model).filter(enabled__in=_f):
            field_value = getattr(obj, rule.field)
            if field_value is None or field_value == "":
                self.validation_error({rule.field: rule.error_message or "This field cannot be blank."})

        # Unique rules
        for rule in UniqueValidationRule.objects.get_for_model(self.model).filter(enabled__in=_f):
            field_value = getattr(obj, rule.field)
            if field_value:
                # Exclude the current object from the count
                count_excluding_current = (
                    obj.__class__._default_manager.filter(**{rule.field: field_value}).exclude(pk=obj.pk).count()  # pylint: disable=protected-access
                )

                if count_excluding_current >= rule.max_instances:
                    self.validation_error(
                        {
                            rule.field: rule.error_message
                            or f"There can only be {rule.max_instances} instance{pluralize(rule.max_instances)} with this value."
                        }
                    )

        # DataComplianceRules
        for compliance_class in get_data_compliance_rules_map().get(self.model, []):
            compliance_class(obj).clean()

        for repo in GitRepository.objects.filter(
            provided_contents__contains="nautobot_data_validation_engine.data_compliance_rules"
        ):
            for compliance_class in get_classes_from_git_repo(repo):
                if (
                    f"{self.context['object']._meta.app_label}.{self.context['object']._meta.model_name}"
                    != compliance_class.model
                ):
                    continue
                compliance_class(self.context["object"]).clean()

    def get_compliance_result(self, message=None, instance=None, attribute=None, valid=True):
        """Generate a DataCompliance object based on the given parameters."""
        attribute_value = getattr(instance, attribute, None)
        class_name = f"{instance._meta.app_label.capitalize()}{instance._meta.model_name.capitalize()}CustomValidator"

        result, _ = DataCompliance.objects.update_or_create(
            compliance_class_name=class_name,
            content_type=ContentType.objects.get_for_model(instance),
            object_id=instance.id,
            validated_attribute=attribute,
            defaults={
                "last_validation_date": timezone.now(),
                "validated_object_str": str(instance),
                "validated_attribute_value": str(attribute_value) if attribute_value else "",
                "message": message,
                "valid": valid,
            },
        )

        return result


def is_data_compliance_rule(obj):
    """Check to see if object is a DataComplianceRule class instance."""
    return inspect.isclass(obj) and issubclass(obj, DataComplianceRule) and obj is not DataComplianceRule


def get_data_compliance_rules_map():
    """Generate a dictionary of audit rulesets associated to their models."""
    compliance_rulesets = {}
    for validators in registry["plugin_custom_validators"].values():
        for validator in validators:
            if is_data_compliance_rule(validator):
                compliance_rulesets.setdefault(validator.model, [])
                compliance_rulesets[validator.model].append(validator)

    return compliance_rulesets


def get_classes_from_git_repo(repo: GitRepository):
    """Get list of DataComplianceRule classes found within the custom_validators folder of the given repo."""
    ensure_git_repository(repo, head=repo.current_head)
    class_list = []
    for importer, discovered_module_name, _ in pkgutil.iter_modules([f"{repo.filesystem_path}/custom_validators"]):
        if discovered_module_name in sys.modules:
            del sys.modules[discovered_module_name]
        module = importer.find_module(discovered_module_name).load_module(discovered_module_name)
        for _, complance_class in inspect.getmembers(module, is_data_compliance_rule):
            class_list.append(complance_class)
    return class_list


class ComplianceError(ValidationError):
    """A compliance error is raised only when an object fails a compliance check."""


class DataComplianceRule(CustomValidator):
    """Class to handle a set of validation functions."""

    name: Optional[str] = None
    model: str
    result_date: timezone
    enforce = False

    def __init__(self, obj):
        """Initialize an DataComplianceRule object."""
        super().__init__(obj)
        self.name = self.name or self.__class__.__name__
        self.result_date = timezone.now()

    def audit(self):
        """Not implemented. Should raise a ComplianceError if an attribute is found to be invalid."""
        raise NotImplementedError

    def mark_existing_attributes_as_valid(self, exclude_attributes=None):
        """Mark all existing attributes (any that were previously created) as valid=True.

        We call this function after running the audit method to update any attributes that didn't have ComplianceErrors raised to set them as valid.
        We pass in any attributes that had ComplianceErrors raised so that we end up with the list of attributes that should now be valid.
        This doesn't create DataCompliance objects for any fields that have always been valid or not referenced in the DataComplianceRule.
        """
        instance = self.context["object"]
        if not exclude_attributes:
            exclude_attributes = []
        attributes = (
            DataCompliance.objects.filter(
                compliance_class_name=self.name,
                content_type=ContentType.objects.get_for_model(instance),
                object_id=instance.id,
            )
            .exclude(validated_attribute__in=["__all__"] + exclude_attributes)
            .values_list("validated_attribute", flat=True)
        )
        for attribute in attributes:
            self.compliance_result(message=f"{attribute.capitalize()} is valid.", attribute=attribute)

    def clean(self):
        """Override the clean method to run the audit function."""
        try:
            self.audit()
            self.mark_existing_attributes_as_valid()
            self.compliance_result(message=f"All {self.name} class rules for {self.context['object']} are valid.")
        except ComplianceError as ex:
            # Create a list of attributes that had ComplianceErrors raised to exclude from later function call
            exclude_attributes = []
            try:
                for attribute, messages in ex.message_dict.items():
                    # Add attribute to excluded list
                    exclude_attributes.append(attribute)
                    # Create/update DataCompliance object for the given attribute
                    for message in messages:
                        self.compliance_result(message=message, attribute=attribute, valid=False)
            except AttributeError:
                for message in ex.messages:
                    self.compliance_result(message=message, valid=False)
            finally:
                self.mark_existing_attributes_as_valid(exclude_attributes=exclude_attributes)
                self.compliance_result(
                    message=f"One or more {self.name} class rules for {self.context['object']} are not valid.",
                    valid=False,
                )
            if self.enforce:
                raise ex

    @staticmethod
    def compliance_error(message):
        """Raise a Compliance Error with the given message."""
        raise ComplianceError(message)

    def compliance_result(self, message, attribute=None, valid=True):
        """Generate a DataCompliance object based on the given parameters."""
        instance = self.context["object"]
        attribute_value = None
        if attribute:
            attribute_value = getattr(instance, attribute)
        else:
            attribute = "__all__"
        result, _ = DataCompliance.objects.update_or_create(
            compliance_class_name=self.name,
            content_type=ContentType.objects.get_for_model(instance),
            object_id=instance.id,
            validated_attribute=attribute,
            defaults={
                "last_validation_date": self.result_date,
                "validated_object_str": str(instance),
                "validated_attribute_value": str(attribute_value) if attribute_value else "",
                "message": message,
                "valid": valid,
            },
        )
        result.validated_save()


class CustomValidatorIterator:
    """Iterator that generates PluginCustomValidator classes for each model registered in the extras feature query registry 'custom_validators'."""

    def __iter__(self):
        """Return a generator of PluginCustomValidator classes for each registered model."""
        for app_label, models in registry["model_features"]["custom_validators"].items():
            for model in models:
                yield type(
                    f"{app_label.capitalize()}{model.capitalize()}CustomValidator",
                    (BaseValidator,),
                    {"model": f"{app_label}.{model}"},
                )


custom_validators = CustomValidatorIterator()
