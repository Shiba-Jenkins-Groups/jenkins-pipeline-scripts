#!/usr/bin/env python3
"""Offline owner config.xml and routing CLI contracts; no Jenkins or secrets."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET


SPEC = importlib.util.spec_from_file_location("owner_routing_evidence", Path(__file__).with_name("release-evidence.py"))
E = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(E)
REVISION = "a" * 40
OLD = "f5c68a5f0276f542754c69b9854f09be8b484011"
SCRIPT = """@Library('jenkins-pipeline@REVISION') _
prodDeploymentPipeline(
    enabled: true,
    libraryRevision: 'REVISION',
    nodeLabel: 'fixture-owner',
    runtimeRoot: '/fixture/runtime with spaces',
    stateDirectory: '/fixture/state',
    dockerEngineId: 'fixture-engine',
    receiptKeyCredentials: 'fixture-receipt',
    scmCredentials: 'fixture-scm'
)
""".replace("REVISION", REVISION)


def configuration(script=SCRIPT):
    root = ET.Element("flow-definition")
    definition = ET.SubElement(root, "definition", {"class": "org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition"})
    ET.SubElement(definition, "script").text = script
    ET.SubElement(definition, "sandbox").text = "true"
    return ET.tostring(root)


class OwnerLibraryRouting(unittest.TestCase):
    def test_exact_literal_pins_and_harmless_comments_pass(self):
        variants = [SCRIPT, SCRIPT.replace("'", '"'), SCRIPT + ";\n",
            "// @Library('jenkins-pipeline@" + OLD + "') _\n" + SCRIPT,
            SCRIPT.replace("prodDeploymentPipeline(", "/* libraryRevision: '" + OLD + "' */\nprodDeploymentPipeline("),
            SCRIPT.replace("nodeLabel:", "// libraryRevision: '" + OLD + "'\n    nodeLabel:")]
        for script in variants:
            with self.subTest(script=variants.index(script)):
                self.assertIsNone(E.owner_library_routing(configuration(script), REVISION))

    def test_old_or_partially_activated_owner_is_rejected(self):
        variants = [SCRIPT.replace(REVISION, OLD), SCRIPT.replace(REVISION, OLD, 1),
            SCRIPT.replace("libraryRevision: '" + REVISION, "libraryRevision: '" + OLD)]
        for script in variants:
            with self.subTest(script=variants.index(script)), self.assertRaises(E.gate.InvalidEvidence):
                E.owner_library_routing(configuration(script), REVISION)

    def test_missing_ambiguous_or_dynamic_groovy_declarations_are_rejected(self):
        variants = [
            SCRIPT.replace("    libraryRevision: '" + REVISION + "',\n", ""),
            SCRIPT.replace("    libraryRevision:", "    // libraryRevision:"),
            "// " + SCRIPT,
            SCRIPT.replace("prodDeploymentPipeline(", "@Library('jenkins-pipeline@" + REVISION + "') _\nprodDeploymentPipeline("),
            SCRIPT.replace("    nodeLabel:", "    libraryRevision: '" + REVISION + "',\n    nodeLabel:"),
            SCRIPT + SCRIPT,
            SCRIPT.replace("libraryRevision: '" + REVISION + "'", "libraryRevision: '" + REVISION + "' + ''"),
            SCRIPT.replace("libraryRevision: '" + REVISION + "'", "libraryRevision: someVariable"),
            SCRIPT.replace("@Library('jenkins-pipeline@" + REVISION + "')", '@Library("jenkins-pipeline@${revision}")'),
            SCRIPT.replace("    libraryRevision: '" + REVISION + "',", "    note: \"libraryRevision: '" + REVISION + "'\","),
            SCRIPT.replace("Library('", "Library('\\"),
            SCRIPT + "/* unterminated",
        ]
        for index, script in enumerate(variants):
            with self.subTest(case=index), self.assertRaises(E.gate.InvalidEvidence):
                E.owner_library_routing(configuration(script), REVISION)

    def test_missing_or_ambiguous_xml_definition_is_rejected(self):
        duplicate = ET.fromstring(configuration())
        duplicate.append(ET.fromstring(configuration()).find("definition"))
        duplicate_script = ET.fromstring(configuration())
        ET.SubElement(duplicate_script.find("definition"), "script").text = SCRIPT
        invalid = [b"<flow-definition/>", b"<flow-definition>",
            b"<flow-definition><!-- <definition><script>fake pins</script></definition> --></flow-definition>",
            configuration().replace(b"CpsFlowDefinition", b"CpsScmFlowDefinition"),
            ET.tostring(duplicate), ET.tostring(duplicate_script),
            b'<!DOCTYPE flow-definition [<!ENTITY x "ignored">]>' + configuration(),
            configuration(" " * 65537)]
        for index, xml in enumerate(invalid):
            with self.subTest(case=index), self.assertRaises(E.gate.InvalidEvidence):
                E.owner_library_routing(xml, REVISION)

    def test_expected_revision_must_be_exact(self):
        for revision in [None, "", "main", "a" * 39, "A" * 40, "a" * 41]:
            with self.subTest(revision=revision), self.assertRaises(E.gate.InvalidEvidence):
                E.owner_library_routing(configuration(), revision)


class RoutingCLI(unittest.TestCase):
    def invoke(self, revision=REVISION, owner_xml=None):
        calls = []
        owner_prefix = "/job/shiba-release-automation/job/shiba-go-ditch-api-project-prod-deploy/"
        responses = {
            "/job/shiba-go-ditch-api-project/job/prod/config.xml":
                b"<flow-definition><properties><jenkins.branch.NoTriggerBranchProperty><strategy>NONE</strategy><triggeredBranchesRegex>^$</triggeredBranchesRegex></jenkins.branch.NoTriggerBranchProperty></properties></flow-definition>",
            owner_prefix + "config.xml": configuration() if owner_xml is None else owner_xml,
            owner_prefix + "api/json": json.dumps({"fullName": "shiba-release-automation/shiba-go-ditch-api-project-prod-deploy", "buildable": True}).encode(),
            "/computer/api/json?depth=1": json.dumps({"computer": [{"assignedLabels": [{"name": "fixture-owner"}],
                "offline": False, "temporarilyOffline": False, "numExecutors": 1}]}).encode(),
        }

        def get(base, suffix):
            self.assertEqual(base, "http://jenkins.invalid")
            calls.append(suffix)
            return responses[suffix]

        arguments = ["release-evidence.py", "check-routing", "--jenkins-url", "http://jenkins.invalid",
                     "--deployment-label", "fixture-owner"]
        if revision is not None:
            arguments += ["--library-revision", revision]
        with patch.object(E, "jenkins_get", side_effect=get), patch.object(E.sys, "argv", arguments), \
                contextlib.redirect_stderr(io.StringIO()):
            result = E.main()
        return result, calls

    def test_optional_revision_fetches_fixed_owner_configuration_once(self):
        result, calls = self.invoke()
        self.assertEqual(result, 0)
        self.assertEqual(len(calls), 4)
        self.assertEqual(sum(path.endswith("prod-deploy/config.xml") for path in calls), 1)

    def test_legacy_cli_without_revision_keeps_existing_read_scope(self):
        result, calls = self.invoke(revision=None)
        self.assertEqual(result, 0)
        self.assertEqual(len(calls), 3)
        self.assertFalse(any(path.endswith("prod-deploy/config.xml") for path in calls))

    def test_partial_activation_blocks_before_executor_inventory(self):
        result, calls = self.invoke(owner_xml=configuration(SCRIPT.replace(REVISION, OLD)))
        self.assertEqual(result, 1)
        self.assertEqual(len(calls), 2)
        self.assertFalse(any(path.endswith("/api/json") for path in calls))

    def test_invalid_requested_revision_blocks_before_any_http(self):
        result, calls = self.invoke(revision="main")
        self.assertEqual(result, 1)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
