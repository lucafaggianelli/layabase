import logging
import datetime
import enum
import os.path
import inspect
import dateutil.parser
import copy
import pymongo
import pymongo.errors
from typing import List, Dict
from flask_restplus import fields as flask_restplus_fields, reqparse, inputs
from bson.objectid import ObjectId
from bson.errors import BSONError
import json

from pycommon_database.flask_restplus_errors import ValidationFailed, ModelCouldNotBeFound

logger = logging.getLogger(__name__)


@enum.unique
class IndexType(enum.IntEnum):
    Unique = 1
    Other = 2


class Column:
    """
    Definition of a Mondo Database field.
    """

    def __init__(self, field_type=str, **kwargs):
        """

        :param field_type: Python field type. Default to str.

        :param choices: Restrict valid values to this list of value (or a function providing those values).
        Default to None or the content of the enum if field is an Enum.
        :param default_value: Default value matching type. Default to None.
        :param description: Field description.
        :param index_type: Type of index amongst IndexType enum. Default to None.
        :param allow_none_as_filter: bool value. Default to False.
        :param is_primary_key: bool value. Default to False.
        :param is_nullable: bool value. Default to opposite of is_primary_key, except if it auto increment
        :param is_required: bool value. Default to False.
        :param should_auto_increment: bool value. Default to False. Only valid for int fields.
        TODO Introduce min and max length, regex
        """
        self.field_type = field_type or str
        name = kwargs.pop('name', None)
        if name:
            self._update_name(name)
        self.get_choices = self._to_get_choices(kwargs.pop('choices', None))
        self.default_value = kwargs.pop('default_value', None)
        self.description = kwargs.pop('description', None)
        self.index_type = kwargs.pop('index_type', None)

        self.allow_none_as_filter = bool(kwargs.pop('allow_none_as_filter', False))
        self.is_primary_key = bool(kwargs.pop('is_primary_key', False))
        self.should_auto_increment = bool(kwargs.pop('should_auto_increment', False))
        if self.should_auto_increment and self.field_type is not int:
            raise Exception('Only int fields can be auto incremented.')
        self.is_nullable = bool(kwargs.pop('is_nullable', True))
        if not self.is_nullable:
            if self.should_auto_increment:
                raise Exception('A field cannot be mandatory and auto incremented at the same time.')
            if self.default_value:
                raise Exception('A field cannot be mandatory and having a default value at the same time.')
        else:
            # Field will be optional only if it is not a primary key without default value and not auto incremented
            self.is_nullable = not self.is_primary_key or self.default_value or self.should_auto_increment
        self.is_required = bool(kwargs.pop('is_required', False))

    def _update_name(self, name):
        if '.' in name:
            raise Exception(f'{name} is not a valid name. Dots are not allowed in Mongo field names.')
        self.name = name
        if '_id' == self.name:
            self.field_type = ObjectId

    def _to_get_choices(self, choices):
        """
        Return a function without arguments returning the choices.
        :param choices: A list of choices or a function providing choices (or None).
        """
        if choices:
            return (lambda: choices) if isinstance(choices, list) else choices
        elif isinstance(self.field_type, enum.EnumMeta):
            return lambda: list(self.field_type.__members__.keys())
        return lambda: None

    def __str__(self):
        return f'{self.name}'

    def validate_query(self, model_as_dict: dict) -> dict:
        """
        Validate data queried.
        :return: Validation errors that might have occurred. Empty if no error occurred.
        """
        value = model_as_dict.get(self.name)
        if value is None:
            return {}
        return self._validate_value(value)

    def validate_insert(self, model_as_dict: dict) -> dict:
        """
        Validate data on insert.
        Even if this method is the same one as validate_update, users with a custom field type might want
        to perform a different validation in case of insert and update (typically checking for missing fields)
        :return: Validation errors that might have occurred. Empty if no error occurred.
        """
        value = model_as_dict.get(self.name)
        if value is None:
            if not self.is_nullable:
                return {self.name: ['Missing data for required field.']}
            return {}
        return self._validate_value(value)

    def validate_update(self, model_as_dict: dict) -> dict:
        """
        Validate data on update.
        Even if this method is the same one as validate_insert, users with a custom field type might want
        to perform a different validation in case of insert and update (typically not checking for missing fields)
        :return: Validation errors that might have occurred. Empty if no error occurred.
        """
        value = model_as_dict.get(self.name)
        if value is None:
            if not self.is_nullable:
                return {self.name: ['Missing data for required field.']}
            return {}
        return self._validate_value(value)

    def _validate_value(self, value) -> dict:
        """
        :return: Validation errors that might have occurred. Empty if no error occurred.
        """
        if self.field_type == datetime.datetime:
            if isinstance(value, str):
                try:
                    value = dateutil.parser.parse(value)
                except:
                    return {self.name: ['Not a valid datetime.']}
        elif self.field_type == datetime.date:
            if isinstance(value, str):
                try:
                    value = dateutil.parser.parse(value).date()
                except:
                    return {self.name: ['Not a valid date.']}
        elif isinstance(self.field_type, enum.EnumMeta):
            if isinstance(value, str):
                if value not in self.get_choices():
                    return {self.name: [f'Value "{value}" is not within {self.get_choices()}.']}
                return {}  # Consider string values valid for Enum type
        elif self.field_type == ObjectId:
            if not isinstance(value, ObjectId):
                try:
                    value = ObjectId(value)
                except BSONError as e:
                    return {self.name: [e.args[0]]}
        elif self.field_type == str:
            if isinstance(value, str):
                if self.get_choices() and value not in self.get_choices():
                    return {self.name: [f'Value "{value}" is not within {self.get_choices()}.']}
        elif self.field_type == int:
            if isinstance(value, int):
                if self.get_choices() and value not in self.get_choices():
                    return {self.name: [f'Value "{value}" is not within {self.get_choices()}.']}

        if not isinstance(value, self.field_type):
            return {self.name: [f'Not a valid {self.field_type.__name__}.']}

        return {}

    def deserialize_query(self, model_as_dict: dict):
        """
        Convert this field value to the proper value that can be inserted in Mongo.
        Even if this method is the same one as deserialize_insert, users with a custom field type might want
        to perform a different deserialization in case of insert and update
        :param model_as_dict: Dictionary containing this field value (or not).
        """
        value = model_as_dict.get(self.name)
        if value is None:
            if not self.allow_none_as_filter:
                model_as_dict.pop(self.name, None)
        else:
            model_as_dict[self.name] = self._deserialize_value(value)

    def deserialize_insert(self, model_as_dict: dict):
        """
        Convert this field value to the proper value that can be inserted in Mongo.
        Even if this method is the same one as deserialize_query, users with a custom field type might want
        to perform a different deserialization in case of insert and update
        :param model_as_dict: Dictionary containing this field value (or not).
        """
        value = model_as_dict.get(self.name)
        if value is None:
            # Ensure that None value are not stored to save space
            model_as_dict.pop(self.name, None)
        else:
            model_as_dict[self.name] = self._deserialize_value(value)

    def deserialize_update(self, model_as_dict: dict):
        """
        Convert this field value to the proper value that can be inserted in Mongo.
        Even if this method is the same one as deserialize_insert, users with a custom field type might want
        to perform a different deserialization in case of insert and update
        :param model_as_dict: Dictionary containing this field value (or not).
        """
        value = model_as_dict.get(self.name)
        if value is None:
            # Ensure that None value are not stored to save space
            model_as_dict.pop(self.name, None)
        else:
            model_as_dict[self.name] = self._deserialize_value(value)

    def _deserialize_value(self, value):
        """
        Convert this field value to the proper value that can be inserted in Mongo.
        :param value: Received field value.
        :return Mongo valid value.
        """
        if value is None:
            return None

        if self.field_type == datetime.datetime:
            if isinstance(value, str):
                value = dateutil.parser.parse(value)
        elif self.field_type == datetime.date:
            if isinstance(value, str):
                value = dateutil.parser.parse(value)
            elif isinstance(value, datetime.date):
                # dates cannot be stored in Mongo, use datetime instead
                if not isinstance(value, datetime.datetime):
                    value = datetime.datetime.combine(value, datetime.datetime.min.time())
                # Ensure that time is not something else than midnight
                else:
                    value = datetime.datetime.combine(value.date(), datetime.datetime.min.time())
        elif isinstance(self.field_type, enum.EnumMeta):
            # Enum cannot be stored in Mongo, use enum value instead
            if isinstance(value, enum.Enum):
                value = value.value
            elif isinstance(value, str):
                value = self.field_type[value].value
        elif self.field_type == ObjectId:
            if not isinstance(value, ObjectId):
                value = ObjectId(value)

        return value

    def serialize(self, model_as_dict: dict):
        value = model_as_dict.get(self.name)

        if value is None:
            if self.is_nullable:
                model_as_dict[self.name] = self.default_value  # Make sure value is set in any case
            return

        if self.field_type == datetime.datetime:
            model_as_dict[self.name] = value.isoformat()  # TODO Time Offset is missing to be fully compliant with RFC
        elif self.field_type == datetime.date:
            model_as_dict[self.name] = value.date().isoformat()
        elif isinstance(self.field_type, enum.EnumMeta):
            model_as_dict[self.name] = self.field_type(value).name
        elif self.field_type == ObjectId:
            model_as_dict[self.name] = str(value)


class DictColumn(Column):
    """
    Definition of a Mongo database dictionary field.
    If you do not want to validate the content of this dict, just use a Column(dict) instead.
    """
    def __init__(self, fields: Dict[str, Column], **kwargs):
        """
        :param fields: Fields (or function providing fields) representing dictionary as a dict(str, Column).
        :param default_value: Default value matching type. Default to None.
        :param description: Field description.
        :param index_type: Type of index amongst IndexType enum. Default to None.
        :param allow_none_as_filter: bool value. Default to False.
        :param is_primary_key: bool value. Default to False.
        :param is_nullable: bool value. Default to opposite of is_primary_key, except if it auto increment
        :param is_required: bool value. Default to False.
        :param should_auto_increment: bool value. Default to False. Only valid for int fields.
        """
        kwargs.pop('field_type', None)
        self.get_fields = (lambda model_as_dict: fields) if (isinstance(fields, dict) and fields) else fields
        if not self.get_fields:
            raise Exception('fields is a mandatory parameter.')
        Column.__init__(self, field_type=dict, **kwargs)

    def _description_model(self, model_as_dict: dict):
        """
        :param model_as_dict: Data provided by the user or empty in case this method is called in another context.
        :return: A CRUDModel describing every dictionary fields.
        """
        class FakeModel(CRUDModel):
            pass
        
        for name, column in self.get_fields(model_as_dict or {}).items():
            column._update_name(name)
            FakeModel.__fields__.append(column)
        
        return FakeModel

    def get_index_fields(self, index_type: IndexType) -> List[Column]:
        return self._description_model({}).get_index_fields(index_type)

    def validate_insert(self, model_as_dict: dict) -> dict:
        errors = Column.validate_insert(self, model_as_dict)
        if not errors:
            value = model_as_dict.get(self.name)
            if value is not None:
                description_model = self._description_model(model_as_dict)
                errors.update({
                    f'{self.name}.{field_name}': field_errors
                    for field_name, field_errors in description_model.validate_insert(value).items()
                })
        return errors

    def deserialize_insert(self, model_as_dict: dict):
        value = model_as_dict.get(self.name)
        if value is None:
            # Ensure that None value are not stored to save space
            model_as_dict.pop(self.name, None)
            return
        self._description_model(model_as_dict).deserialize_insert(value)

    def validate_update(self, model_as_dict: dict) -> dict:
        errors = Column.validate_update(self, model_as_dict)
        if not errors:
            value = model_as_dict.get(self.name)
            if value is not None:
                description_model = self._description_model(model_as_dict)
                errors.update({
                    f'{self.name}.{field_name}': field_errors
                    for field_name, field_errors in description_model.validate_update(value).items()
                })
        return errors

    def deserialize_update(self, model_as_dict: dict):
        value = model_as_dict.get(self.name)
        if value is None:
            # Ensure that None value are not stored to save space
            model_as_dict.pop(self.name, None)
        else:
            self._description_model(model_as_dict).deserialize_update(value)

    def validate_query(self, model_as_dict: dict) -> dict:
        errors = Column.validate_query(self, model_as_dict)
        if not errors:
            value = model_as_dict.get(self.name)
            if value is not None:
                description_model = self._description_model(model_as_dict)
                errors.update({
                    f'{self.name}.{field_name}': field_errors
                    for field_name, field_errors in description_model.validate_query(value).items()
                })
        return errors

    def deserialize_query(self, model_as_dict: dict):
        value = model_as_dict.get(self.name)
        if value is None:
            if not self.allow_none_as_filter:
                model_as_dict.pop(self.name, None)
        else:
            self._description_model(model_as_dict).deserialize_query(value)

    def serialize(self, model_as_dict: dict):
        value = model_as_dict.get(self.name, {})
        self._description_model(model_as_dict).serialize(value)


class ListColumn(Column):
    """
    Definition of a Mongo database list field.
    This list should only contains items of a specified type.
    If you do not want to validate the content of this list, just use a Column(list) instead.
    """
    def __init__(self, list_item_type: Column, **kwargs):
        """
        :param list_item_type: Column describing an element of this list.

        :param default_value: Default value matching type. Default to None.
        :param description: Field description.
        :param index_type: Type of index amongst IndexType enum. Default to None.
        :param allow_none_as_filter: bool value. Default to False.
        :param is_primary_key: bool value. Default to False.
        :param is_nullable: bool value. Default to opposite of is_primary_key, except if it auto increment
        :param is_required: bool value. Default to False.
        :param should_auto_increment: bool value. Default to False. Only valid for int fields.
        """
        kwargs.pop('field_type', None)
        self.list_item_column = list_item_type
        Column.__init__(self, field_type=list, **kwargs)

    def _update_name(self, name):
        Column._update_name(self, name)
        self.list_item_column._update_name(name)

    def validate_insert(self, model_as_dict: dict) -> dict:
        errors = Column.validate_insert(self, model_as_dict)
        if not errors:
            values = model_as_dict.get(self.name) or []
            for index, value in enumerate(values):
                errors.update({
                    f'{field_name}[{index}]': field_errors
                    for field_name, field_errors in self.list_item_column.validate_insert({self.name: value}).items()
                })
        return errors

    def deserialize_insert(self, model_as_dict: dict):
        values = model_as_dict.get(self.name)
        if values is None:
            # Ensure that None value are not stored to save space
            model_as_dict.pop(self.name, None)
        else:
            new_values = []
            for value in values:
                value_dict = {self.name: value}
                self.list_item_column.deserialize_insert(value_dict)
                if self.name in value_dict:
                    new_values.append(value_dict[self.name])

            model_as_dict[self.name] = new_values

    def validate_update(self, model_as_dict: dict) -> dict:
        errors = Column.validate_update(self, model_as_dict)
        if not errors:
            values = model_as_dict[self.name]
            for index, value in enumerate(values):
                errors.update({
                    f'{field_name}[{index}]': field_errors
                    for field_name, field_errors in self.list_item_column.validate_update({self.name: value}).items()
                })
        return errors

    def deserialize_update(self, model_as_dict: dict):
        values = model_as_dict.get(self.name)
        if values is None:
            # Ensure that None value are not stored to save space
            model_as_dict.pop(self.name, None)
        else:
            new_values = []
            for value in values:
                value_dict = {self.name: value}
                self.list_item_column.deserialize_update(value_dict)
                if self.name in value_dict:
                    new_values.append(value_dict[self.name])

            model_as_dict[self.name] = new_values

    def validate_query(self, model_as_dict: dict) -> dict:
        errors = Column.validate_query(self, model_as_dict)
        if not errors:
            values = model_as_dict.get(self.name) or []
            for index, value in enumerate(values):
                errors.update({
                    f'{field_name}[{index}]': field_errors
                    for field_name, field_errors in self.list_item_column.validate_query({self.name: value}).items()
                })
        return errors

    def deserialize_query(self, model_as_dict: dict):
        values = model_as_dict.get(self.name)
        if values is None:
            if not self.allow_none_as_filter:
                model_as_dict.pop(self.name, None)
        else:
            new_values = []
            for value in values:
                value_dict = {self.name: value}
                self.list_item_column.deserialize_query(value_dict)
                if self.name in value_dict:
                    new_values.append(value_dict[self.name])

            model_as_dict[self.name] = new_values

    def serialize(self, model_as_dict: dict):
        values = model_as_dict.get(self.name, [])
        new_values = []
        for value in values:
            value_dict = {self.name: value}
            self.list_item_column.serialize(value_dict)
            if self.name in value_dict:
                new_values.append(value_dict[self.name])

        model_as_dict[self.name] = new_values


def to_mongo_field(attribute):
    attribute[1]._update_name(attribute[0])
    return attribute[1]


class CRUDModel:
    """
    Class providing CRUD helper methods for a Mongo model.
    __collection__ class property must be specified in Model.
    __counters__ class property must be specified in Model.
    Calling load_from(...) will provide you those properties.
    """
    __tablename__ = None  # Name of the collection described by this model
    __collection__ = None  # Mongo collection
    __counters__ = None  # Mongo counters collection (to increment fields)
    __fields__: List[Column] = []  # All Mongo fields within this model
    audit_model = None

    def __init_subclass__(cls, base=None, table_name: str=None, audit: bool=False, **kwargs):
        super().__init_subclass__(**kwargs)
        cls.__tablename__ = table_name
        cls.__fields__ = [to_mongo_field(attribute) for attribute in inspect.getmembers(cls) if isinstance(attribute[1], Column)]
        if base is not None:  # Allow to not provide base to create fake models
            cls.__collection__ = base[cls.__tablename__]
            cls.__counters__ = base['counters']
            cls._create_indexes(IndexType.Unique)
            cls._create_indexes(IndexType.Other)
        if audit:
            from pycommon_database.audit_mongo import _create_from
            cls.audit_model = _create_from(cls, base)

    @classmethod
    def _create_indexes(cls, index_type: IndexType):
        """
        Create indexes of specified type.
        """
        try:
            criteria = [(field.name, pymongo.ASCENDING) for field in cls.get_index_fields(index_type)]
            if criteria:
                # Avoid using auto generated index name that might be too long
                index_name = f'uidx{cls.__collection__.name}' if index_type == IndexType.Unique else f'idx{cls.__collection__.name}'
                logger.info(f"Create {index_name} {index_type} index on {cls.__collection__.name} using {criteria} criteria.")
                cls.__collection__.create_index(criteria, unique=index_type == IndexType.Unique, name=index_name)
        except pymongo.errors.DuplicateKeyError:
            logger.exception(f'Duplicate key found for {criteria} criteria when creating a {index_type} index.')
            raise

    @classmethod
    def get_index_fields(cls, index_type: IndexType) -> List[Column]:
        """
        In case a field is a dictionary and some fields within it should be indexed, override this method.
        """
        index_fields = [field for field in cls.__fields__ if field.index_type == index_type]
        for field in cls.__fields__:
            if isinstance(field, DictColumn):
                index_fields.extend(field.get_index_fields(index_type))
        return index_fields

    @classmethod
    def get(cls, **model_to_query) -> dict:
        """
        Return a model formatted as a dictionary.
        """
        errors = cls.validate_query(model_to_query)
        if errors:
            raise ValidationFailed(model_to_query, errors)

        cls.deserialize_query(model_to_query)

        if cls.__collection__.count(model_to_query) > 1:
            raise ValidationFailed(model_to_query, message='More than one result: Consider another filtering.')

        model = cls.__collection__.find_one(model_to_query)
        return cls.serialize(model)  # Convert Cursor to dict

    @classmethod
    def get_all(cls, **model_to_query) -> List[dict]:
        """
        Return all models formatted as a list of dictionaries.
        """
        limit = model_to_query.pop('limit', 0) or 0
        offset = model_to_query.pop('offset', 0) or 0
        errors = cls.validate_query(model_to_query)
        if errors:
            raise ValidationFailed(model_to_query, errors)

        cls.deserialize_query(model_to_query)

        models = cls.__collection__.find(model_to_query, skip=offset, limit=limit)
        return [cls.serialize(model) for model in models]  # Convert Cursor to dict

    @classmethod
    def rollback_to(cls, **model_to_query) -> int:
        """
        All records matching the query and valid at specified validity will be considered as valid.
        :return Number of records updated.
        """
        return 0

    @classmethod
    def validate_query(cls, model_as_dict: dict) -> dict:
        """
        Validate data queried.
        :return: Validation errors that might have occurred. Empty if no error occurred.
        """
        queried_fields_names = [field.name for field in cls.__fields__ if field.name in model_as_dict]
        unknown_fields = [field_name for field_name in model_as_dict if field_name not in queried_fields_names]
        new_model_as_dict = copy.deepcopy(model_as_dict)  # Is there really a need for a deep copy here?
        for unknown_field in unknown_fields:
            # Convert dot notation fields into known fields to be able to validate them
            known_field, field_value = cls._handle_dot_notation(unknown_field, new_model_as_dict[unknown_field])
            if known_field:
                previous_dict = new_model_as_dict.setdefault(known_field.name, {})
                previous_dict.update(field_value)

        errors = {}

        for field in [field for field in cls.__fields__ if field.name in new_model_as_dict]:
            errors.update(field.validate_query(new_model_as_dict))

        return errors

    @classmethod
    def deserialize_query(cls, model_as_dict: dict):
        queried_fields_names = [field.name for field in cls.__fields__ if field.name in model_as_dict]
        unknown_fields = [field_name for field_name in model_as_dict if field_name not in queried_fields_names]
        dot_model_as_dict = {}

        for unknown_field in unknown_fields:
            known_field, field_value = cls._handle_dot_notation(unknown_field, model_as_dict[unknown_field])
            del model_as_dict[unknown_field]
            if known_field:
                previous_dict = dot_model_as_dict.setdefault(known_field.name, {})
                previous_dict.update(field_value)
            else:
                logger.warning(f'Skipping unknown field {unknown_field}.')

        # Deserialize dot notation values
        for field in [field for field in cls.__fields__ if field.name in dot_model_as_dict]:
            field.deserialize_query(dot_model_as_dict)
            # Put back deserialized values as dot notation fields
            for inner_field_name, value in dot_model_as_dict[field.name].items():
                model_as_dict[f'{field.name}.{inner_field_name}'] = value

        for field in [field for field in cls.__fields__ if field.name in model_as_dict]:
            field.deserialize_query(model_as_dict)

    @classmethod
    def _handle_dot_notation(cls, field_name: str, value) -> (Column, dict):
        parent_field = field_name.split('.', maxsplit=1)
        if len(parent_field) == 2:
            for field in cls.__fields__:
                if field.name == parent_field[0] and field.field_type == dict:
                    return field, {parent_field[1]: value}
        return None, None

    @classmethod
    def serialize(cls, model_as_dict: dict) -> dict:
        if not model_as_dict:
            return {}

        for field in cls.__fields__:
            field.serialize(model_as_dict)

        # Make sure fields that were stored in a previous version of a model are not returned if removed since then
        # It also ensure _id can be skipped unless specified otherwise in the model
        known_fields = [field.name for field in cls.__fields__]
        removed_fields = [field_name for field_name in model_as_dict if field_name not in known_fields]
        if removed_fields:
            for removed_field in removed_fields:
                del model_as_dict[removed_field]
            logger.debug(f'Skipping removed fields {removed_fields}.')

        return model_as_dict

    @classmethod
    def add(cls, model_as_dict: dict) -> dict:
        """
        Add a model formatted as a dictionary.
        :raises ValidationFailed in case validation fail.
        :returns The inserted model formatted as a dictionary.
        """
        errors = cls.validate_insert(model_as_dict)
        if errors:
            raise ValidationFailed(model_as_dict, errors)

        cls.deserialize_insert(model_as_dict)
        try:
            cls._insert_one(model_as_dict)
            if cls.audit_model:
                cls.audit_model.audit_add(model_as_dict)
            return cls.serialize(model_as_dict)
        except pymongo.errors.DuplicateKeyError:
            raise ValidationFailed(cls.serialize(model_as_dict), message='This item already exists.')

    @classmethod
    def add_all(cls, models_as_list_of_dict: List[dict]) -> List[dict]:
        """
        Add models formatted as a list of dictionaries.
        :raises ValidationFailed in case validation fail.
        :returns The inserted models formatted as a list of dictionaries.
        """
        if not models_as_list_of_dict:
            raise ValidationFailed([], message='No data provided.')

        new_models_as_list_of_dict = copy.deepcopy(models_as_list_of_dict)

        errors = {}

        for index, model_as_dict in enumerate(new_models_as_list_of_dict):
            model_errors = cls.validate_insert(model_as_dict)
            if model_errors:
                errors[index] = model_errors
                continue

            cls.deserialize_insert(model_as_dict)

        if errors:
            raise ValidationFailed(models_as_list_of_dict, errors)

        try:
            cls._insert_many(new_models_as_list_of_dict)
            if cls.audit_model:
                for inserted_dict in new_models_as_list_of_dict:
                    cls.audit_model.audit_add(inserted_dict)
            return [cls.serialize(model) for model in new_models_as_list_of_dict]
        except pymongo.errors.BulkWriteError as e:
            raise ValidationFailed(models_as_list_of_dict, message=str(e.details))

    @classmethod
    def validate_insert(cls, model_as_dict: dict) -> dict:
        """
        Validate data on insert.
        :return: Validation errors that might have occurred. Empty if no error occurred.
        """
        if not model_as_dict:
            return {'': ['No data provided.']}

        new_model_as_dict = copy.deepcopy(model_as_dict)

        field_names = [field.name for field in cls.__fields__]
        unknown_fields = [field_name for field_name in new_model_as_dict if field_name not in field_names]
        for unknown_field in unknown_fields:
            known_field, field_value = cls._handle_dot_notation(unknown_field, new_model_as_dict[unknown_field])
            if known_field:
                previous_dict = new_model_as_dict.setdefault(known_field.name, {})
                previous_dict.update(field_value)

        errors = {}

        for field in cls.__fields__:
            errors.update(field.validate_insert(new_model_as_dict))

        return errors

    @classmethod
    def deserialize_insert(cls, model_as_dict: dict):
        """
        Update this model dictionary by ensuring that it contains only valid Mongo values.
        """
        field_names = [field.name for field in cls.__fields__]
        unknown_fields = [field_name for field_name in model_as_dict if field_name not in field_names]
        for unknown_field in unknown_fields:
            known_field, field_value = cls._handle_dot_notation(unknown_field, model_as_dict[unknown_field])
            del model_as_dict[unknown_field]
            if known_field:
                previous_dict = model_as_dict.setdefault(known_field.name, {})
                previous_dict.update(field_value)
            else:
                logger.warning(f'Skipping unknown field {unknown_field}.')

        for field in cls.__fields__:
            field.deserialize_insert(model_as_dict)
            if field.should_auto_increment:
                model_as_dict[field.name] = cls._increment(field.name)

    @classmethod
    def _increment(cls, field_name: str):
        counter_key = {'_id': cls.__collection__.name}
        counter_update = {'$inc': {'%s.counter' % field_name: 1},
                          '$set': {'%s.timestamp' % field_name: datetime.datetime.utcnow().isoformat()}}
        counter_element = cls.__counters__.find_one_and_update(counter_key, counter_update,
                                                               return_document=pymongo.ReturnDocument.AFTER,
                                                               upsert=True)
        return counter_element[field_name]['counter']

    @classmethod
    def update(cls, model_as_dict: dict) -> (dict, dict):
        """
        Update a model formatted as a dictionary.
        :raises ValidationFailed in case validation fail.
        :returns A tuple containing previous model formatted as a dictionary (first item)
        and new model formatted as a dictionary (second item).
        """
        if not model_as_dict:
            raise ValidationFailed({}, message='No data provided.')

        errors = cls.validate_update(model_as_dict)
        if errors:
            raise ValidationFailed(model_as_dict, errors)

        cls.deserialize_update(model_as_dict)

        previous_model_as_dict, new_model_as_dict = cls._update_one(model_as_dict)

        if cls.audit_model:
            cls.audit_model.audit_update(new_model_as_dict)
        return cls.serialize(previous_model_as_dict), cls.serialize(new_model_as_dict)

    @classmethod
    def validate_update(cls, model_as_dict: dict) -> dict:
        """
        Validate data on update.
        :return: Validation errors that might have occurred. Empty if no error occurred.
        """
        if not model_as_dict:
            return {'': ['No data provided.']}

        new_model_as_dict = copy.deepcopy(model_as_dict)

        updated_field_names = [field.name for field in cls.__fields__ if field.name in new_model_as_dict]
        unknown_fields = [field_name for field_name in new_model_as_dict if field_name not in updated_field_names]
        for unknown_field in unknown_fields:
            # Convert dot notation fields into known fields to be able to validate them
            known_field, field_value = cls._handle_dot_notation(unknown_field, new_model_as_dict[unknown_field])
            if known_field:
                previous_dict = new_model_as_dict.setdefault(known_field.name, {})
                previous_dict.update(field_value)

        errors = {}

        # Also ensure that Primary keys will contain a valid value
        updated_fields = [field for field in cls.__fields__ if field.name in new_model_as_dict or field.is_primary_key]
        for field in updated_fields:
            errors.update(field.validate_update(new_model_as_dict))

        return errors

    @classmethod
    def deserialize_update(cls, model_as_dict: dict):
        """
        Update this model dictionary by ensuring that it contains only valid Mongo values.
        """
        updated_field_names = [field.name for field in cls.__fields__ if field.name in model_as_dict]
        unknown_fields = [field_name for field_name in model_as_dict if field_name not in updated_field_names]
        dot_model_as_dict = {}

        for unknown_field in unknown_fields:
            known_field, field_value = cls._handle_dot_notation(unknown_field, model_as_dict[unknown_field])
            del model_as_dict[unknown_field]
            if known_field:
                previous_dict = dot_model_as_dict.setdefault(known_field.name, {})
                previous_dict.update(field_value)
            else:
                logger.warning(f'Skipping unknown field {unknown_field}.')

        # Deserialize dot notation values
        for field in [field for field in cls.__fields__ if field.name in dot_model_as_dict]:
            field.deserialize_update(dot_model_as_dict)
            # Put back deserialized values as dot notation fields
            for inner_field_name, value in dot_model_as_dict[field.name].items():
                model_as_dict[f'{field.name}.{inner_field_name}'] = value

        updated_fields = [field for field in cls.__fields__ if field.name in model_as_dict or field.is_primary_key]
        for field in updated_fields:
            field.deserialize_update(model_as_dict)

    @classmethod
    def remove(cls, **model_to_query) -> int:
        """
        Remove the model(s) matching those criterion.
        :returns Number of removed rows.
        """
        errors = cls.validate_query(model_to_query)
        if errors:
            raise ValidationFailed(model_to_query, errors)

        cls.deserialize_query(model_to_query)

        if cls.audit_model:
            cls.audit_model.audit_remove(**model_to_query)
        return cls._delete_many(model_to_query)

    @classmethod
    def _insert_many(cls, models_as_list_of_dict: List[dict]):
        cls.__collection__.insert_many(models_as_list_of_dict)

    @classmethod
    def _insert_one(cls, model_as_dict: dict) -> dict:
        cls.__collection__.insert_one(model_as_dict)
        return model_as_dict

    @classmethod
    def _update_one(cls, model_as_dict: dict) -> (dict, dict):
        model_as_dict_keys = cls._to_primary_keys_model(model_as_dict)
        previous_model_as_dict = cls.__collection__.find_one(model_as_dict_keys)
        if not previous_model_as_dict:
            raise ModelCouldNotBeFound(model_as_dict_keys)

        cls.__collection__.update_one(model_as_dict_keys, {'$set': model_as_dict})
        return previous_model_as_dict, cls.__collection__.find_one(model_as_dict_keys)

    @classmethod
    def _delete_many(cls, model_to_query: dict) -> int:
        return cls.__collection__.delete_many(model_to_query).deleted_count

    @classmethod
    def _to_primary_keys_model(cls, model_as_dict: dict) -> dict:
        # TODO Compute primary keys only once
        primary_key_field_names = [field.name for field in cls.__fields__ if field.is_primary_key]
        return {field_name: value for field_name, value in model_as_dict.items() if
                field_name in primary_key_field_names}

    @classmethod
    def query_get_parser(cls):
        query_get_parser = cls._query_parser()
        query_get_parser.add_argument('limit', type=inputs.positive)
        query_get_parser.add_argument('offset', type=inputs.natural)
        return query_get_parser

    @classmethod
    def query_delete_parser(cls):
        return cls._query_parser()

    @classmethod
    def query_rollback_parser(cls):
        return  # Only VersioningCRUDModel allows rollback

    @classmethod
    def _query_parser(cls):
        query_parser = reqparse.RequestParser()
        for field in cls.__fields__:
            cls._add_field_to_query_parser(query_parser, field)
        return query_parser

    @classmethod
    def _add_field_to_query_parser(cls, query_parser, field: Column, prefix=''):
        if isinstance(field, DictColumn):
            # Describe every dict column field as dot notation
            for inner_field in field._description_model({}).__fields__:
                cls._add_field_to_query_parser(query_parser, inner_field, f'{field.name}.')
        elif isinstance(field, ListColumn):
            # Note that List of dict or list of list might be wrongly parsed
            query_parser.add_argument(
                f'{prefix}{field.name}',
                required=field.is_required,
                type=_get_python_type(field.list_item_column),
                action='append'
            )
        elif field.field_type == list:
            query_parser.add_argument(
                f'{prefix}{field.name}',
                required=field.is_required,
                type=str,  # Consider anything as valid, thus consider as str in query
                action='append'
            )
        else:
            query_parser.add_argument(
                f'{prefix}{field.name}',
                required=field.is_required,
                type=_get_python_type(field)
            )

    @classmethod
    def description_dictionary(cls) -> dict:
        description = {
            'collection': cls.__tablename__,
        }
        for field in cls.__fields__:
            description[field.name] = field.name
        return description

    @classmethod
    def json_post_model(cls, namespace):
        return cls._model_with_all_fields(namespace)

    @classmethod
    def json_put_model(cls, namespace):
        return cls._model_with_all_fields(namespace)

    @classmethod
    def get_response_model(cls, namespace):
        return cls._model_with_all_fields(namespace)

    @classmethod
    def _model_with_all_fields(cls, namespace):
        return namespace.model(cls.__name__, cls._flask_restplus_fields(namespace))

    @classmethod
    def get_audit_response_model(cls, namespace):
        if cls.audit_model:
            return namespace.model('Audit' + cls.__name__, cls.audit_model._flask_restplus_fields(namespace))

    @classmethod
    def _flask_restplus_fields(cls, namespace) -> dict:
        return {field.name: cls._to_flask_restplus_field(namespace, field) for field in cls.__fields__}

    @classmethod
    def _to_flask_restplus_field(cls, namespace, field: Column):
        if isinstance(field, DictColumn):
            dict_fields = field._description_model({})._flask_restplus_fields(namespace)
            if dict_fields:
                dict_model = namespace.model('_'.join(dict_fields), dict_fields)
                # Nested field cannot contains nothing
                return flask_restplus_fields.Nested(
                    dict_model,
                    required=field.is_required,
                    example=_get_example(field),
                    description=field.description,
                    enum=field.get_choices(),
                    default=field.default_value,
                    readonly=field.should_auto_increment,
                )
            else:
                return _get_flask_restplus_type(field)(
                    required=field.is_required,
                    example=_get_example(field),
                    description=field.description,
                    enum=field.get_choices(),
                    default=field.default_value,
                    readonly=field.should_auto_increment,
                )
        elif isinstance(field, ListColumn):
            return flask_restplus_fields.List(
                cls._to_flask_restplus_field(namespace, field.list_item_column),
                required=field.is_required,
                example=_get_example(field),
                description=field.description,
                enum=field.get_choices(),
                default=field.default_value,
                readonly=field.should_auto_increment,
            )
        elif field.field_type == list:
            return flask_restplus_fields.List(
                flask_restplus_fields.String,
                required=field.is_required,
                example=_get_example(field),
                description=field.description,
                enum=field.get_choices(),
                default=field.default_value,
                readonly=field.should_auto_increment,
            )
        else:
            return _get_flask_restplus_type(field)(
                required=field.is_required,
                example=_get_example(field),
                description=field.description,
                enum=field.get_choices(),
                default=field.default_value,
                readonly=field.should_auto_increment,
            )

    @classmethod
    def flask_restplus_description_fields(cls) -> dict:
        exported_fields = {
            'collection': flask_restplus_fields.String(required=True, example='collection',
                                                       description='Collection name'),
        }

        exported_fields.update({
            field.name: flask_restplus_fields.String(
                required=field.is_required,
                example='column',
                description=field.description,
            )
            for field in cls.__fields__
        })
        return exported_fields


def _load(database_connection_url: str, create_models_func: callable):
    """
    Create all necessary tables and perform the link between models and underlying database connection.

    :param database_connection_url: URL formatted as a standard database connection string (Mandatory).
    :param create_models_func: Function that will be called to create models and return them (instances of CRUDModel)
    (Mandatory).
    """
    logger.info(f'Connecting to {database_connection_url}...')
    database_name = os.path.basename(database_connection_url)
    if database_connection_url.startswith('mongomock'):
        import mongomock  # This is a test dependency only
        client = mongomock.MongoClient()
    else:
        client = pymongo.MongoClient(database_connection_url)
    base = client[database_name]
    logger.debug(f'Creating models...')
    create_models_func(base)
    return base


def _reset(base):
    """
    If the database was already created, then drop all tables and recreate them all.
    """
    if base:
        for collection in base._collections.values():
            _reset_collection(base, collection)


def _reset_collection(base, collection):
    """
    Reset collection and keep indexes.
    """
    logger.info(f'Resetting all data related to "{collection.name}" collection...')
    nb_removed = collection.delete_many({}).deleted_count
    logger.info(f'{nb_removed} records deleted.')

    logger.info(f'Resetting counters."{collection.name}".')
    nb_removed = base['counters'].delete_many({'_id': collection.name}).deleted_count
    logger.info(f'{nb_removed} counter records deleted')


def _get_flask_restplus_type(field: Column):
    """
    Return the Flask RestPlus field type (as a class) corresponding to this Mongo field.
    Default to String.
    """
    if field.field_type == int:
        return flask_restplus_fields.Integer
    if field.field_type == float:
        return flask_restplus_fields.Float
    if field.field_type == bool:
        return flask_restplus_fields.Boolean
    if field.field_type == datetime.date:
        return flask_restplus_fields.Date
    if field.field_type == datetime.datetime:
        return flask_restplus_fields.DateTime
    if field.field_type == list:
        return flask_restplus_fields.List
    if field.field_type == dict:
        return flask_restplus_fields.Raw

    return flask_restplus_fields.String


def _get_example(field: Column):
    if field.default_value is not None:
        return field.default_value

    if isinstance(field, DictColumn):
        return (
            {
                dict_key.name: _get_example(dict_key)
                for dict_key in field._description_model({}).__fields__
            }
        )

    if isinstance(field, ListColumn):
        return [_get_example(field.list_item_column)]

    return field.get_choices()[0] if field.get_choices() else _get_default_example(field)


def _get_default_example(field: Column):
    """
    Return an Example value corresponding to this Mongodb field.
    """
    if field.field_type == int:
        return 1
    if field.field_type == float:
        return 1.4
    if field.field_type == bool:
        return True
    if field.field_type == datetime.date:
        return '2017-09-24'
    if field.field_type == datetime.datetime:
        return '2017-09-24T15:36:09'
    if field.field_type == list:
        return [
            f'1st {field.name} sample',
            f'2nd {field.name} sample',
        ]
    if field.field_type == dict:
        return {
            f'1st {field.name} key': f'1st {field.name} sample',
            f'2nd {field.name} key': f'2nd {field.name} sample',
        }
    if field.field_type == ObjectId:
        return '1234567890QBCDEF01234567'
    return f'sample {field.name}'


def _get_python_type(field: Column):
    """
    Return the Python type corresponding to this Mongo field.

    :raises Exception if field type is not managed yet.
    """
    if field.field_type == bool:
        return inputs.boolean
    if field.field_type == datetime.date:
        return inputs.date_from_iso8601
    if field.field_type == datetime.datetime:
        return inputs.datetime_from_iso8601
    if isinstance(field.field_type, enum.EnumMeta):
        return str
    if field.field_type == dict:
        return json.loads
    if field.field_type == list:
        return json.loads
    if field.field_type == ObjectId:
        return str

    return field.field_type
