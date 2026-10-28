#!/usr/bin/env python3
"""Render a disabled central Jenkins job XML; never contact or mutate Jenkins."""
import argparse
from pathlib import Path
import re
import xml.etree.ElementTree as E


def render(library_revision, approver, builder_engine_id):
    if not re.fullmatch(r'[a-f0-9]{40}', library_revision) or not re.fullmatch(r'[A-Za-z0-9_.@-]{1,80}', approver) or not re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', builder_engine_id):
        raise ValueError('reviewed immutable library and Jenkins user ID required')
    root = E.Element('flow-definition', {'plugin': 'workflow-job'})
    E.SubElement(root, 'actions')
    E.SubElement(root, 'description').text = 'LOCAL Recognition immutable artifact only; reviewed definition, no deployment or PROD downstream. Disabled until separately authorized.'
    E.SubElement(root, 'keepDependencies').text = 'false'
    E.SubElement(root, 'properties')
    definition = E.SubElement(root, 'definition', {'class': 'org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition', 'plugin': 'workflow-cps'})
    E.SubElement(definition, 'script').text = (
        "@Library('jenkins-pipeline@" + library_revision + "') _\n"
        "recognitionLocalArtifactPipeline(enabled: false, libraryRevision: '" + library_revision +
        "', sourceCommit: '9d0400d003de68be827524d21543200bf78bfdfd', approvers: ['" + approver +
        "'], builderEngineId: '" + builder_engine_id + "')\n")
    E.SubElement(definition, 'sandbox').text = 'true'
    E.SubElement(root, 'triggers')
    E.SubElement(root, 'disabled').text = 'true'
    return E.tostring(root, encoding='utf-8', xml_declaration=True) + b'\n'


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--library-revision', required=True)
    p.add_argument('--approver', required=True)
    p.add_argument('--builder-engine-id', required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    raw = render(a.library_revision, a.approver, a.builder_engine_id)
    with a.output.open('xb') as f:
        f.write(raw)
