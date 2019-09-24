import flask
import flask_restplus
import pytest
from layaberr import ValidationFailed

from layabase import database, database_mongo


class TestLimitsController(database.CRUDController):
    pass


def _create_models(base):
    class TestLimitsModel(
        database_mongo.CRUDModel, base=base, table_name="limits_table_name"
    ):
        key = database_mongo.Column(is_primary_key=True, min_length=3, max_length=4)
        list_field = database_mongo.Column(
            list, min_length=2, max_length=3, example=["my", "test"]
        )
        dict_field = database_mongo.Column(
            dict, min_length=2, max_length=3, example={"my": 1, "test": 2}
        )
        int_field = database_mongo.Column(int, min_value=100, max_value=999)
        float_field = database_mongo.Column(float, min_value=1.25, max_value=1.75)

    TestLimitsController.model(TestLimitsModel)

    return [TestLimitsModel]


@pytest.fixture
def db():
    _db = database.load("mongomock", _create_models)
    yield _db
    database.reset(_db)


@pytest.fixture
def app(db):
    application = flask.Flask(__name__)
    application.testing = True
    api = flask_restplus.Api(application)
    namespace = api.namespace("Test", path="/")

    TestLimitsController.namespace(namespace)

    @namespace.route("/test")
    class TestResource(flask_restplus.Resource):
        @namespace.expect(TestLimitsController.query_get_parser)
        @namespace.marshal_with(TestLimitsController.get_response_model)
        def get(self):
            return []

        @namespace.expect(TestLimitsController.json_post_model)
        def post(self):
            return []

        @namespace.expect(TestLimitsController.json_put_model)
        def put(self):
            return []

        @namespace.expect(TestLimitsController.query_delete_parser)
        def delete(self):
            return []

    return application


class DateTimeModuleMock:
    class DateTimeMock:
        @staticmethod
        def utcnow():
            class UTCDateTimeMock:
                @staticmethod
                def isoformat():
                    return "2018-10-11T15:05:05.663979"

            return UTCDateTimeMock

    datetime = DateTimeMock


def test_within_limits_is_valid(db):
    assert {
        "dict_field": {"my": 1, "test": 2},
        "int_field": 100,
        "float_field": 1.3,
        "key": "111",
        "list_field": ["1", "2", "3"],
    } == TestLimitsController.post(
        {
            "dict_field": {"my": 1, "test": 2},
            "key": "111",
            "list_field": ["1", "2", "3"],
            "int_field": 100,
            "float_field": 1.3,
        }
    )


def test_outside_upper_limits_is_invalid(db):
    with pytest.raises(ValidationFailed) as exception_info:
        TestLimitsController.post(
            {
                "key": "11111",
                "list_field": ["1", "2", "3", "4", "5"],
                "int_field": 1000,
                "float_field": 1.1,
                "dict_field": {"my": 1, "test": 2, "is": 3, "invalid": 4},
            }
        )
    assert {
        "int_field": ['Value "1000" is too big. Maximum value is 999.'],
        "key": ['Value "11111" is too big. Maximum length is 4.'],
        "float_field": ['Value "1.1" is too small. Minimum value is 1.25.'],
        "list_field": [
            "['1', '2', '3', '4', '5'] contains too many values. Maximum length is 3."
        ],
        "dict_field": [
            "{'my': 1, 'test': 2, 'is': 3, 'invalid': 4} contains too many values. Maximum length is 3."
        ],
    } == exception_info.value.errors
    assert {
        "int_field": 1000,
        "float_field": 1.1,
        "key": "11111",
        "list_field": ["1", "2", "3", "4", "5"],
        "dict_field": {"my": 1, "test": 2, "is": 3, "invalid": 4},
    } == exception_info.value.received_data


def test_outside_lower_limits_is_invalid(db):
    with pytest.raises(ValidationFailed) as exception_info:
        TestLimitsController.post(
            {
                "key": "11",
                "list_field": ["1"],
                "int_field": 99,
                "dict_field": {"my": 1},
                "float_field": 2.1,
            }
        )
    assert {
        "dict_field": [
            "{'my': 1} does not contains enough values. Minimum length is 2."
        ],
        "int_field": ['Value "99" is too small. Minimum value is 100.'],
        "float_field": ['Value "2.1" is too big. Maximum value is 1.75.'],
        "key": ['Value "11" is too small. Minimum length is 3.'],
        "list_field": ["['1'] does not contains enough values. Minimum length is 2."],
    } == exception_info.value.errors
    assert {
        "key": "11",
        "list_field": ["1"],
        "int_field": 99,
        "dict_field": {"my": 1},
        "float_field": 2.1,
    } == exception_info.value.received_data


def test_open_api_definition(client):
    response = client.get("/swagger.json")
    assert response.json == {
        "swagger": "2.0",
        "basePath": "/",
        "paths": {
            "/test": {
                "post": {
                    "responses": {"200": {"description": "Success"}},
                    "operationId": "post_test_resource",
                    "parameters": [
                        {
                            "name": "payload",
                            "required": True,
                            "in": "body",
                            "schema": {"$ref": "#/definitions/TestLimitsModel"},
                        }
                    ],
                    "tags": ["Test"],
                },
                "put": {
                    "responses": {"200": {"description": "Success"}},
                    "operationId": "put_test_resource",
                    "parameters": [
                        {
                            "name": "payload",
                            "required": True,
                            "in": "body",
                            "schema": {"$ref": "#/definitions/TestLimitsModel"},
                        }
                    ],
                    "tags": ["Test"],
                },
                "delete": {
                    "responses": {"200": {"description": "Success"}},
                    "operationId": "delete_test_resource",
                    "parameters": [
                        {
                            "name": "dict_field",
                            "in": "query",
                            "type": "array",
                            "items": {"type": "string"},
                            "collectionFormat": "multi",
                        },
                        {
                            "name": "float_field",
                            "in": "query",
                            "type": "array",
                            "items": {"type": "number"},
                            "collectionFormat": "multi",
                        },
                        {
                            "name": "int_field",
                            "in": "query",
                            "type": "array",
                            "items": {"type": "integer"},
                            "collectionFormat": "multi",
                        },
                        {
                            "name": "key",
                            "in": "query",
                            "type": "array",
                            "items": {"type": "string"},
                            "collectionFormat": "multi",
                        },
                        {
                            "name": "list_field",
                            "in": "query",
                            "type": "array",
                            "items": {"type": "string"},
                            "collectionFormat": "multi",
                        },
                    ],
                    "tags": ["Test"],
                },
                "get": {
                    "responses": {
                        "200": {
                            "description": "Success",
                            "schema": {"$ref": "#/definitions/TestLimitsModel"},
                        }
                    },
                    "operationId": "get_test_resource",
                    "parameters": [
                        {
                            "name": "dict_field",
                            "in": "query",
                            "type": "array",
                            "items": {"type": "string"},
                            "collectionFormat": "multi",
                        },
                        {
                            "name": "float_field",
                            "in": "query",
                            "type": "array",
                            "items": {"type": "number"},
                            "collectionFormat": "multi",
                        },
                        {
                            "name": "int_field",
                            "in": "query",
                            "type": "array",
                            "items": {"type": "integer"},
                            "collectionFormat": "multi",
                        },
                        {
                            "name": "key",
                            "in": "query",
                            "type": "array",
                            "items": {"type": "string"},
                            "collectionFormat": "multi",
                        },
                        {
                            "name": "list_field",
                            "in": "query",
                            "type": "array",
                            "items": {"type": "string"},
                            "collectionFormat": "multi",
                        },
                        {
                            "name": "limit",
                            "in": "query",
                            "type": "integer",
                            "minimum": 0,
                            "exclusiveMinimum": True,
                        },
                        {
                            "name": "offset",
                            "in": "query",
                            "type": "integer",
                            "minimum": 0,
                        },
                        {
                            "name": "X-Fields",
                            "in": "header",
                            "type": "string",
                            "format": "mask",
                            "description": "An optional fields mask",
                        },
                    ],
                    "tags": ["Test"],
                },
            }
        },
        "info": {"title": "API", "version": "1.0"},
        "produces": ["application/json"],
        "consumes": ["application/json"],
        "tags": [{"name": "Test"}],
        "definitions": {
            "TestLimitsModel": {
                "properties": {
                    "dict_field": {
                        "type": "object",
                        "readOnly": False,
                        "example": {"my": 1, "test": 2},
                    },
                    "float_field": {
                        "type": "number",
                        "readOnly": False,
                        "example": 1.4,
                        "minimum": 1.25,
                        "maximum": 1.75,
                    },
                    "int_field": {
                        "type": "integer",
                        "readOnly": False,
                        "example": 100,
                        "minimum": 100,
                        "maximum": 999,
                    },
                    "key": {
                        "type": "string",
                        "readOnly": False,
                        "example": "XXX",
                        "minLength": 3,
                        "maxLength": 4,
                    },
                    "list_field": {
                        "type": "array",
                        "readOnly": False,
                        "example": ["my", "test"],
                        "minItems": 2,
                        "maxItems": 3,
                        "items": {"type": "string"},
                    },
                },
                "type": "object",
            }
        },
        "responses": {
            "ParseError": {"description": "When a mask can't be parsed"},
            "MaskError": {"description": "When any error occurs on mask"},
        },
    }
