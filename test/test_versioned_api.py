# Copyright 2020-present MongoDB, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from __future__ import annotations

import sys
from test import UnitTest

sys.path[0:0] = [""]

from test import unittest

from pymongo.mongo_client import MongoClient
from pymongo.server_api import ServerApi, ServerApiVersion


class TestServerApi(UnitTest):
    def test_server_api_defaults(self):
        api = ServerApi(ServerApiVersion.V1)
        self.assertEqual(api.version, "1")
        self.assertIsNone(api.strict)
        self.assertIsNone(api.deprecation_errors)

    def test_server_api_explicit_false(self):
        api = ServerApi("1", strict=False, deprecation_errors=False)
        self.assertEqual(api.version, "1")
        self.assertFalse(api.strict)
        self.assertFalse(api.deprecation_errors)

    def test_server_api_strict(self):
        api = ServerApi("1", strict=True, deprecation_errors=True)
        self.assertEqual(api.version, "1")
        self.assertTrue(api.strict)
        self.assertTrue(api.deprecation_errors)

    def test_server_api_validation(self):
        with self.assertRaises(ValueError):
            ServerApi("2")
        with self.assertRaises(TypeError):
            ServerApi("1", strict="not-a-bool")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            ServerApi("1", deprecation_errors="not-a-bool")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            MongoClient(server_api="not-a-ServerApi")

    def assertServerApi(self, event):
        self.assertIn("apiVersion", event.command)
        self.assertEqual(event.command["apiVersion"], "1")

    def assertNoServerApi(self, event):
        self.assertNotIn("apiVersion", event.command)

    def assertServerApiInAllCommands(self, events):
        for event in events:
            self.assertServerApi(event)


if __name__ == "__main__":
    unittest.main()
