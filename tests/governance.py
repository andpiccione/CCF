# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the Apache 2.0 License.
import os
import http
import subprocess
import infra.network
import infra.path
import infra.proc
import infra.net
import infra.e2e_args
import suite.test_requirements as reqs
import infra.logging_app as app
import json
import jinja2
import requests
import infra.crypto
from datetime import datetime
import governance_js
from infra.runner import ConcurrentRunner
import governance_history

from loguru import logger as LOG


@reqs.description("Test create endpoint is not available")
def test_create_endpoint(network, args):
    primary, _ = network.find_nodes()
    with primary.client() as c:
        r = c.post("/node/create")
        assert r.status_code == http.HTTPStatus.FORBIDDEN.value
        assert r.body.json()["error"]["message"] == "Node is not in initial state."
    return network


@reqs.description("Test consensus status")
def test_consensus_status(network, args):
    primary, _ = network.find_nodes()
    with primary.client() as c:
        r = c.get("/node/consensus")
        assert r.status_code == http.HTTPStatus.OK.value
        assert r.body.json()["details"]["leadership_state"] == "Leader"
    return network


@reqs.description("Test quotes")
@reqs.supports_methods("/app/quotes/self", "/app/quotes")
def test_quote(network, args):
    if args.enclave_type == "virtual":
        LOG.warning("Quote test can only run in real enclaves, skipping")
        return network

    primary, _ = network.find_nodes()
    with primary.client() as c:
        oed = subprocess.run(
            [
                os.path.join(args.oe_binary, "oesign"),
                "dump",
                "-e",
                infra.path.build_lib_path(args.package, args.enclave_type),
            ],
            capture_output=True,
            check=True,
        )
        lines = [
            line
            for line in oed.stdout.decode().split(os.linesep)
            if line.startswith("mrenclave=")
        ]
        expected_mrenclave = lines[0].strip().split("=")[1]

        r = c.get("/node/quotes/self")
        primary_quote_info = r.body.json()
        assert primary_quote_info["node_id"] == primary.node_id
        primary_mrenclave = primary_quote_info["mrenclave"]
        assert primary_mrenclave == expected_mrenclave, (
            primary_mrenclave,
            expected_mrenclave,
        )

        r = c.get("/node/quotes")
        quotes = r.body.json()["quotes"]
        assert len(quotes) == len(network.get_joined_nodes())

        for quote in quotes:
            mrenclave = quote["mrenclave"]
            assert mrenclave == expected_mrenclave, (mrenclave, expected_mrenclave)

            cafile = os.path.join(network.common_dir, "service_cert.pem")
            assert (
                infra.proc.ccall(
                    "verify_quote.sh",
                    f"https://{primary.get_public_rpc_host()}:{primary.get_public_rpc_port()}",
                    "--cacert",
                    f"{cafile}",
                    log_output=True,
                ).returncode
                == 0
            ), f"Quote verification for node {quote['node_id']} failed"

    return network


@reqs.description("Add user, remove user")
@reqs.supports_methods("/app/log/private")
def test_user(network, args, verify=True):
    # Note: This test should not be chained in the test suite as it creates
    # a new user and uses its own LoggingTxs
    primary, _ = network.find_nodes()
    new_user_local_id = "user3"
    new_user = network.create_user(new_user_local_id, args.participants_curve)
    user_data = {"lifetime": "temporary"}
    network.consortium.add_user(primary, new_user.local_id, user_data)
    txs = app.LoggingTxs(user_id=new_user.local_id)
    txs.issue(
        network=network,
        number_txs=1,
    )
    if verify:
        txs.verify()
    network.consortium.remove_user(primary, new_user.service_id)
    with primary.client(new_user_local_id) as c:
        r = c.get("/app/log/private")
        assert r.status_code == http.HTTPStatus.UNAUTHORIZED.value
    return network


@reqs.description("Validate sample Jinja templates")
@reqs.supports_methods("/app/log/private")
def test_jinja_templates(network, args, verify=True):
    primary, _ = network.find_primary()

    new_user_local_id = "bob"
    new_user = network.create_user(new_user_local_id, args.participants_curve)

    with primary.client(new_user_local_id) as c:
        r = c.post("/app/log/private", {"id": 42, "msg": "New user test"})
        assert r.status_code == http.HTTPStatus.UNAUTHORIZED.value

    template_loader = jinja2.ChoiceLoader(
        [
            jinja2.FileSystemLoader(args.jinja_templates_path),
            jinja2.FileSystemLoader(os.path.dirname(new_user.cert_path)),
        ]
    )
    template_env = jinja2.Environment(
        loader=template_loader, undefined=jinja2.StrictUndefined
    )

    proposal_template = template_env.get_template("set_user_proposal.json.jinja")
    proposal_body = proposal_template.render(cert=os.path.basename(new_user.cert_path))
    proposal = network.consortium.get_any_active_member().propose(
        primary, proposal_body
    )

    ballot_template = template_env.get_template("ballot.json.jinja")
    ballot_body = ballot_template.render(**json.loads(proposal_body))
    network.consortium.vote_using_majority(primary, proposal, ballot_body)

    with primary.client(new_user_local_id) as c:
        r = c.post("/app/log/private", {"id": 42, "msg": "New user test"})
        assert r.status_code == http.HTTPStatus.OK.value

    network.consortium.remove_user(primary, new_user.service_id)
    with primary.client(new_user_local_id) as c:
        r = c.get("/app/log/private")
        assert r.status_code == http.HTTPStatus.UNAUTHORIZED.value

    return network


@reqs.description("Add untrusted node, check no quote is returned")
def test_no_quote(network, args):
    untrusted_node = network.create_node(
        infra.interfaces.HostSpec(
            rpc_interfaces={
                infra.interfaces.PRIMARY_RPC_INTERFACE: infra.interfaces.RPCInterface(
                    endorsement=infra.interfaces.Endorsement(authority="Node")
                )
            }
        )
    )
    network.join_node(untrusted_node, args.package, args)
    with untrusted_node.client(
        ca=os.path.join(
            untrusted_node.common_dir, f"{untrusted_node.local_node_id}.pem"
        )
    ) as uc:
        r = uc.get("/node/quotes/self")
        assert r.status_code == http.HTTPStatus.NOT_FOUND
    return network


@reqs.description("Check member data")
def test_member_data(network, args):
    assert args.initial_operator_count > 0
    latest_public_tables, _ = network.get_latest_ledger_public_state()
    members_info = latest_public_tables["public:ccf.gov.members.info"]

    md_count = 0
    for member in network.get_members():
        stored_member_info = json.loads(members_info[member.service_id.encode()])
        if member.member_data:
            assert (
                stored_member_info["member_data"] == member.member_data
            ), f'stored member data "{stored_member_info["member_data"]}" != expected "{member.member_data} "'
            md_count += 1
        else:
            assert "member_data" not in stored_member_info

    assert md_count == args.initial_operator_count

    return network


@reqs.description("Check /gov/members endpoint")
def test_all_members(network, args):
    def run_test_all_members(network):
        primary, _ = network.find_primary()

        with primary.client() as c:
            r = c.get("/gov/members")
            assert r.status_code == http.HTTPStatus.OK.value
            response_members = r.body.json()

        network_members = network.get_members()
        assert len(network_members) == len(response_members)

        for member in network_members:
            assert member.service_id in response_members
            response_details = response_members[member.service_id]
            assert response_details["cert"] == member.cert
            assert (
                infra.member.MemberStatus(response_details["status"])
                == member.status_code
            )
            assert response_details["member_data"] == member.member_data
            if member.is_recovery_member:
                recovery_enc_key = open(
                    member.member_info["encryption_public_key_file"], encoding="utf-8"
                ).read()
                assert response_details["public_encryption_key"] == recovery_enc_key
            else:
                assert response_details["public_encryption_key"] is None

    # Test on current network
    run_test_all_members(network)

    # Test on mid-recovery network
    primary, _ = network.find_primary()
    current_ledger_dir, committed_ledger_dirs = primary.get_ledger()
    snapshots_dir = network.get_committed_snapshots(primary)
    network.stop_all_nodes()
    recovered_network = infra.network.Network(
        args.nodes,
        args.binary_dir,
        args.debug_nodes,
        args.perf_nodes,
        existing_network=network,
    )
    recovered_network.start_in_recovery(
        args,
        ledger_dir=current_ledger_dir,
        committed_ledger_dirs=committed_ledger_dirs,
        snapshots_dir=snapshots_dir,
    )
    run_test_all_members(recovered_network)
    recovered_network.recover(args)

    return recovered_network


@reqs.description("Test ack state digest updates")
def test_ack_state_digest_update(network, args):
    for node in network.get_joined_nodes():
        network.consortium.get_any_active_member().update_ack_state_digest(node)
    return network


@reqs.description("Test invalid client signatures")
def test_invalid_client_signature(network, args):
    primary, _ = network.find_primary()

    def post_proposal_request_raw(node, headers=None, expected_error_msg=None):
        r = requests.post(
            f"https://{node.get_public_rpc_host()}:{node.get_public_rpc_port()}/gov/proposals",
            headers=headers,
            verify=os.path.join(node.common_dir, "service_cert.pem"),
        ).json()
        assert r["error"]["code"] == "InvalidAuthenticationInfo"
        assert (
            expected_error_msg in r["error"]["message"]
        ), f"Expected error message '{expected_error_msg}' not in '{r['error']['message']}'"

    # Verify that _some_ HTTP signature parsing errors are communicated back to the client
    post_proposal_request_raw(
        primary,
        headers=None,
        expected_error_msg="Missing signature",
    )
    post_proposal_request_raw(
        primary,
        headers={"Authorization": "invalid"},
        expected_error_msg="'authorization' header only contains one field",
    )
    post_proposal_request_raw(
        primary,
        headers={"Authorization": "invalid invalid"},
        expected_error_msg="'authorization' scheme for signature should be 'Signature",
    )
    post_proposal_request_raw(
        primary,
        headers={"Authorization": "Signature invalid"},
        expected_error_msg="Error verifying HTTP 'digest' header: Missing 'digest' header",
    )


@reqs.description("Renew certificates of all nodes, one by one")
def test_each_node_cert_renewal(network, args):
    primary, _ = network.find_primary()
    now = datetime.now()
    validity_period_allowed = args.maximum_node_certificate_validity_days - 1
    validity_period_forbidden = args.maximum_node_certificate_validity_days + 1

    test_vectors = [
        (now, validity_period_allowed, None),
        (now, None, None),  # Omit validity period (deduced from service configuration)
        (now, -1, infra.proposal.ProposalNotCreated),
        (now, validity_period_forbidden, infra.proposal.ProposalNotAccepted),
    ]

    for (valid_from, validity_period_days, expected_exception) in test_vectors:
        for node in network.get_joined_nodes():
            with node.client() as c:
                c.get("/node/network/nodes")

                node_cert_tls_before = node.get_tls_certificate_pem()
                assert (
                    infra.crypto.compute_public_key_der_hash_hex_from_pem(
                        node_cert_tls_before
                    )
                    == node.node_id
                )

                try:
                    valid_from_x509 = str(infra.crypto.datetime_to_X509time(valid_from))
                    network.consortium.set_node_certificate_validity(
                        primary,
                        node,
                        valid_from=valid_from_x509,
                        validity_period_days=validity_period_days,
                    )
                    node.set_certificate_validity_period(
                        valid_from_x509,
                        validity_period_days
                        or args.maximum_node_certificate_validity_days,
                    )
                except Exception as e:
                    if expected_exception is None:
                        raise e
                    assert isinstance(e, expected_exception)
                    continue
                else:
                    assert (
                        expected_exception is None
                    ), "Proposal should have not succeeded"

                # Node certificate is updated on global commit hook
                network.wait_for_all_nodes_to_commit(primary)

                node_cert_tls_after = node.get_tls_certificate_pem()
                assert (
                    node_cert_tls_before != node_cert_tls_after
                ), f"Node {node.local_node_id} certificate was not renewed"
                node.verify_certificate_validity_period()
                LOG.info(
                    f"Certificate for node {node.local_node_id} has successfully been renewed"
                )

                # Long-connected client is still connected after certificate renewal
                c.get("/node/network/nodes")

    return network


def renew_service_certificate(network, args, valid_from, validity_period_days):
    primary, _ = network.find_primary()
    valid_from_x509 = str(infra.crypto.datetime_to_X509time(valid_from))
    network.consortium.set_service_certificate_validity(
        primary,
        valid_from=valid_from_x509,
        validity_period_days=validity_period_days,
    )
    network.verify_service_certificate_validity_period(
        validity_period_days or args.maximum_service_certificate_validity_days
    )
    return network


@reqs.description("Renew service certificate")
def test_service_cert_renewal(network, args):
    return renew_service_certificate(
        network,
        args,
        valid_from=datetime.now(),
        validity_period_days=args.maximum_service_certificate_validity_days - 1,
    )


@reqs.description("Renew service certificate - extended")
def test_service_cert_renewal_extended(network, args):

    validity_period_forbidden = args.maximum_service_certificate_validity_days + 1

    now = datetime.now()
    test_vectors = [
        (now, None, None),  # Omit validity period (deduced from service configuration)
        (now, -1, infra.proposal.ProposalNotCreated),
        (now, validity_period_forbidden, infra.proposal.ProposalNotAccepted),
    ]

    for (valid_from, validity_period_days, expected_exception) in test_vectors:
        try:
            renew_service_certificate(network, args, valid_from, validity_period_days)
        except Exception as e:
            assert isinstance(e, expected_exception)
            continue
        else:
            assert expected_exception is None, "Proposal should have not succeeded"

    return network


@reqs.description("Update certificates of all nodes, one by one")
def test_all_nodes_cert_renewal(network, args):
    primary, _ = network.find_primary()

    valid_from = str(infra.crypto.datetime_to_X509time(datetime.now()))
    validity_period_days = args.maximum_node_certificate_validity_days

    network.consortium.set_all_nodes_certificate_validity(
        primary,
        valid_from=valid_from,
        validity_period_days=validity_period_days,
    )

    for node in network.get_joined_nodes():
        node.set_certificate_validity_period(valid_from, validity_period_days)


def gov(args):
    with infra.network.network(
        args.nodes, args.binary_dir, args.debug_nodes, args.perf_nodes, pdb=args.pdb
    ) as network:
        network.start_and_open(args)
        network.consortium.set_authenticate_session(args.authenticate_session)
        test_create_endpoint(network, args)
        test_consensus_status(network, args)
        test_member_data(network, args)
        network = test_all_members(network, args)
        test_quote(network, args)
        test_user(network, args)
        test_jinja_templates(network, args)
        test_no_quote(network, args)
        test_ack_state_digest_update(network, args)
        test_invalid_client_signature(network, args)
        test_each_node_cert_renewal(network, args)
        test_all_nodes_cert_renewal(network, args)
        test_service_cert_renewal(network, args)
        test_service_cert_renewal_extended(network, args)


def js_gov(args):
    with infra.network.network(
        args.nodes, args.binary_dir, args.debug_nodes, args.perf_nodes, pdb=args.pdb
    ) as network:
        network.start_and_open(args)
        governance_js.test_proposal_validation(network, args)
        governance_js.test_proposal_storage(network, args)
        governance_js.test_proposal_withdrawal(network, args)
        governance_js.test_ballot_storage(network, args)
        governance_js.test_pure_proposals(network, args)
        governance_js.test_proposals_with_votes(network, args)
        governance_js.test_vote_failure_reporting(network, args)
        governance_js.test_operator_proposals_and_votes(network, args)
        governance_js.test_apply(network, args)
        governance_js.test_actions(network, args)
        governance_js.test_set_constitution(network, args)


if __name__ == "__main__":

    def add(parser):
        parser.add_argument(
            "--jinja-templates-path",
            help="Path to directory containing sample Jinja templates",
            required=True,
        )

    cr = ConcurrentRunner(add)

    cr.add(
        "session_auth",
        gov,
        package="samples/apps/logging/liblogging",
        nodes=infra.e2e_args.max_nodes(cr.args, f=0),
        initial_user_count=3,
        authenticate_session=True,
    )

    cr.add(
        "session_noauth",
        gov,
        package="samples/apps/logging/liblogging",
        nodes=infra.e2e_args.max_nodes(cr.args, f=0),
        initial_user_count=3,
        authenticate_session=False,
    )

    cr.add(
        "js",
        js_gov,
        package="samples/apps/logging/liblogging",
        nodes=infra.e2e_args.max_nodes(cr.args, f=0),
        initial_user_count=3,
        authenticate_session=True,
    )

    cr.add(
        "history",
        governance_history.run,
        package="samples/apps/logging/liblogging",
        nodes=infra.e2e_args.max_nodes(cr.args, f=0),
    )

    cr.run(2)
