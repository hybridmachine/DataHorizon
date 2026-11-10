# SPDX-License-Identifier: GPL-3.0-only
import unittest
from datahorizon.service import openapi
try:
    from openapi_schema_validator import OAS30Validator
    from openapi_spec_validator import validate
    from jsonschema.exceptions import ValidationError
except ImportError:OAS30Validator=None


@unittest.skipIf(OAS30Validator is None,'Optional build-only OpenAPI validators unavailable')
class AutomationOpenApiTests(unittest.TestCase):
    def test_code_action_examples_and_catalog_schemas(self):
        document=openapi();validate(document);schemas=document['components']['schemas']
        for name in ('ModuleSaveOperation','ModuleDeleteOperation','MacroRunOperation'):
            validator=OAS30Validator({**schemas[name],'components':document['components']});validator.validate(schemas[name]['example'])
            for required in schemas[name]['required']:
                malformed=dict(schemas[name]['example']);malformed.pop(required,None)
                with self.assertRaises(ValidationError):validator.validate(malformed)
            malformed={**schemas[name]['example'],'approved':True}
            with self.assertRaises(ValidationError):validator.validate(malformed)
        self.assertEqual(document['paths']['/v1/workbooks/{workbook}/code']['post']['requestBody']['content']['application/json']['schema']['$ref'],'#/components/schemas/CodeReadRequest')


if __name__=='__main__':unittest.main()
