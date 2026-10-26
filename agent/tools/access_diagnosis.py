"""Strands tool: explain an AccessDenied failure and prove the smallest fix.

CloudTrail records *that* a call was denied. This tool works out *why*, using the
IAM policy simulator (read-only), and when a permission is simply missing it
proposes the narrowest statement that would allow exactly the denied call, then
re-runs the simulator with that statement to prove it works.

Every denial in the run with the same principal + action + resource is treated
as one diagnosis, so thousands of repeated failures collapse into one cause.
The verdict, proposed policy and verification are produced here, never by the
model. Nothing is changed in the account.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from functools import lru_cache

import boto3
from botocore.exceptions import ClientError
from strands import tool

from agent.models.diagnosis import AccessDiagnosis, DiagnosisVerdict
from agent.models.event import NormalizedEvent
from agent.tools.context import get_analysis_context, record_tool_call

logger = logging.getLogger(__name__)

DENIED_CODES = {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation",
                "Client.UnauthorizedOperation", "UnauthorizedAccess"}

# "User: <arn> is not authorized to perform: <action> [on resource: <res>] [because|with <reason>]"
_MESSAGE_RE = re.compile(
    r"User: (?P<arn>arn:[^\s]+) is not authorized to perform: (?P<action>[\w-]+:[\w*]+)"
    r"(?: on resource: (?P<resource>[^\s]+))?(?: (?:because|with) (?P<reason>.+?))?\s*$"
)
_ASSUMED_ROLE_RE = re.compile(r"^arn:(?P<part>[\w-]+):sts::(?P<acct>\d{12}):assumed-role/(?P<role>[^/]+)/")
# Event sources whose IAM prefix differs from the hostname.
_PREFIX_OVERRIDES = {"monitoring": "cloudwatch", "email": "ses", "runtime.sagemaker": "sagemaker"}
_MAX_EVIDENCE = 5
# Strands may run tool calls concurrently; IDs and the cache must be assigned atomically.
_LOCK = threading.Lock()


def is_access_denied(event: NormalizedEvent) -> bool:
    return (event.error_code or "") in DENIED_CODES


@lru_cache(maxsize=1)
def _iam():
    return boto3.client("iam")


def _kms(region: str):
    return boto3.client("kms", region_name=region)


def _parse(event: NormalizedEvent) -> dict:
    """Principal ARN, action, resource and AWS's stated reason for one denial."""
    message = (event.error or "").split(": ", 1)[-1]
    match = _MESSAGE_RE.search(message)
    if match:
        return {k: (v.strip() if v else None) for k, v in match.groupdict().items()}
    service = event.event_source.split(".amazonaws.com")[0]
    prefix = _PREFIX_OVERRIDES.get(service, service)
    name = re.sub(r"\d{8}(v\d+)?$", "", event.event_name)  # UpdateFunctionCode20150331v2
    return {"arn": None, "action": f"{prefix}:{name}", "resource": None, "reason": None}


def _group_key(event: NormalizedEvent) -> tuple:
    parsed = _parse(event)
    role = parsed["arn"]
    if role:
        m = _ASSUMED_ROLE_RE.match(role)
        role = m.group("role") if m else role
    else:
        role = (event.principal.name or "").split("/")[0]
    return (role, parsed["action"], parsed["resource"] or "*")


@lru_cache(maxsize=256)
def _principal_arn(caller_arn: str | None, principal_name: str | None, principal_type: str | None) -> str | None:
    """IAM role/user ARN the simulator can evaluate (it cannot take an STS session ARN)."""
    if caller_arn and ":iam::" in caller_arn:
        return caller_arn
    role_name = None
    if caller_arn:
        m = _ASSUMED_ROLE_RE.match(caller_arn)
        role_name = m.group("role") if m else None
    elif principal_type == "AssumedRole" and principal_name:
        role_name = principal_name.split("/")[0]
    if role_name:
        return _iam().get_role(RoleName=role_name)["Role"]["Arn"]
    if principal_type == "IAMUser" and principal_name:
        return _iam().get_user(UserName=principal_name)["User"]["Arn"]
    return None


def _simulate(principal_arn: str, action: str, resource: str, extra_policy: dict | None = None,
              resource_policy: str | None = None) -> dict:
    kwargs = {"PolicySourceArn": principal_arn, "ActionNames": [action], "ResourceArns": [resource]}
    if extra_policy:
        kwargs["PolicyInputList"] = [json.dumps(extra_policy)]
    if resource_policy:
        kwargs["ResourcePolicy"] = resource_policy
    return _iam().simulate_principal_policy(**kwargs)["EvaluationResults"][0]


def _key_policy(resource: str) -> str | None:
    """KMS denials usually hinge on the key policy; include it when we can read it."""
    if not resource.startswith("arn:aws:kms:") or ":key/" not in resource:
        return None
    try:
        return _kms(resource.split(":")[3]).get_key_policy(KeyId=resource, PolicyName="default")["Policy"]
    except ClientError as exc:
        logger.info("cannot read key policy for %s: %s", resource, exc.response["Error"]["Code"])
        return None


def _deny_sources(result: dict) -> list[str]:
    return sorted({s.get("SourcePolicyId") or s.get("SourcePolicyType", "unknown")
                   for s in result.get("MatchedStatements", [])})


def _guardrails(result: dict) -> list[str]:
    blocked = []
    if result.get("PermissionsBoundaryDecisionDetail", {}).get("AllowedByPermissionsBoundary") is False:
        blocked.append("permissions boundary")
    if result.get("OrganizationsDecisionDetail", {}).get("AllowedByOrganizations") is False:
        blocked.append("organization SCP")
    return blocked


def _minimal_policy(action: str, resource: str) -> dict:
    return {"Version": "2012-10-17", "Statement": [
        {"Sid": "ArchivalAgentProposedFix", "Effect": "Allow", "Action": action, "Resource": resource}]}


def diagnose(events: list[NormalizedEvent], all_events: list[NormalizedEvent], diagnosis_id: str,
             episode_ids_by_event: dict[str, str]) -> AccessDiagnosis:
    """Diagnose one (principal, action, resource) denial group. Never raises."""
    first = events[0]
    parsed = _parse(first)
    key = _group_key(first)
    matching = sorted((e for e in all_events if is_access_denied(e) and _group_key(e) == key),
                      key=lambda e: e.event_time)
    base = dict(
        diagnosisId=diagnosis_id, action=parsed["action"], resource=parsed["resource"] or "*",
        awsErrorReason=parsed["reason"], occurrences=len(matching),
        episodeIds=sorted({episode_ids_by_event[e.event_id] for e in matching if e.event_id in episode_ids_by_event}),
        firstSeen=matching[0].event_time, lastSeen=matching[-1].event_time,
        evidenceEventIds=[e.event_id for e in matching[:_MAX_EVIDENCE]],
    )
    caller = parsed["arn"] or ""
    if "/aws-service-role/" in caller or (first.principal.name or "").startswith("AWSServiceRoleFor"):
        return AccessDiagnosis(**base, verdict=DiagnosisVerdict.AWS_MANAGED_ROLE, principalArn=caller or None,
                               explanation="The caller is an AWS service-linked role. Its permissions are managed "
                                           "by AWS and cannot be edited, so this denial is not a customer fix.")
    try:
        principal_arn = _principal_arn(parsed["arn"], first.principal.name, first.principal.type)
    except ClientError as exc:
        return AccessDiagnosis(**base, verdict=DiagnosisVerdict.CANNOT_DIAGNOSE,
                               explanation=f"Could not resolve the IAM principal ({exc.response['Error']['Code']}). "
                                           "It may have been deleted since the event.")
    if not principal_arn or not parsed["action"]:
        return AccessDiagnosis(**base, verdict=DiagnosisVerdict.CANNOT_DIAGNOSE,
                               explanation="The event does not identify an IAM role or user the simulator can evaluate.")

    action, resource = parsed["action"], parsed["resource"] or "*"
    key_policy = _key_policy(resource)
    limitations = ["The simulator does not evaluate session policies or VPC endpoint policies."]
    if resource.startswith("arn:aws:kms:") and not key_policy:
        limitations.append("The KMS key policy could not be read, so only identity policies were evaluated.")
    try:
        now = _simulate(principal_arn, action, resource, resource_policy=key_policy)
    except ClientError as exc:
        return AccessDiagnosis(**base, principalArn=principal_arn, verdict=DiagnosisVerdict.CANNOT_DIAGNOSE,
                               explanation=f"The IAM policy simulator rejected the request "
                                           f"({exc.response['Error']['Code']}).", limitations=limitations)
    base.update(principalArn=principal_arn, simulatorDecision=now["EvalDecision"],
                resourcePolicyEvaluated=bool(key_policy), limitations=limitations)
    decision = now["EvalDecision"]

    if decision == "allowed":
        return AccessDiagnosis(**base, verdict=DiagnosisVerdict.NOW_ALLOWED,
                               explanation="Current policies allow this call. Either a policy changed after the event, "
                                           "or the denial came from a policy type the simulator cannot see.")
    if decision == "explicitDeny":
        return AccessDiagnosis(**base, verdict=DiagnosisVerdict.EXPLICIT_DENY, denySources=_deny_sources(now),
                               explanation="An explicit Deny statement matches this call. Adding an Allow would not "
                                           "help; the Deny itself must be reviewed.")

    # Implicit deny: nothing allows it. Propose the narrowest Allow and prove it.
    policy = _minimal_policy(action, resource)
    with_fix = _simulate(principal_arn, action, resource, extra_policy=policy, resource_policy=key_policy)
    if with_fix["EvalDecision"] == "allowed":
        return AccessDiagnosis(**base, verdict=DiagnosisVerdict.MISSING_PERMISSION, proposedPolicy=policy,
                               fixVerified=True, decisionWithFix="allowed",
                               explanation=f"No policy attached to the principal allows {action}. Adding the single "
                                           "statement below makes the simulator return allowed.")
    blockers = _guardrails(with_fix) or (["resource policy"] if key_policy else [])
    return AccessDiagnosis(**base, verdict=DiagnosisVerdict.BLOCKED_BY_GUARDRAIL, proposedPolicy=policy,
                           decisionWithFix=with_fix["EvalDecision"], denySources=blockers,
                           explanation="No identity policy allows this call, and even with an Allow added it stays "
                                       f"denied by: {', '.join(blockers) or 'another policy layer'}.")


@tool
def diagnose_access_denied(episode_id: str) -> dict:
    """Explain WHY calls in an episode were denied (AccessDenied / UnauthorizedOperation)
    and, when a permission is simply missing, the smallest fix, verified by the IAM
    policy simulator.

    Call this for every episode whose failures are AccessDenied or
    UnauthorizedOperation, before you describe or recommend anything about it.
    Identical denials across the whole run (same principal, action, resource) are
    merged into one diagnosis, so one call can explain thousands of events.

    Cite results by diagnosisId (e.g. "AD-1") in facts and recommendations. Do not
    restate the proposed policy JSON; it is rendered from the diagnosis itself.
    A verified fix is a diagnosis, not an instruction: whether the principal SHOULD
    have the permission is a human decision, so frame recommendations as "confirm
    whether X needs Y; if so, the verified minimal fix is in AD-n".

    Args:
        episode_id: The episode identifier from the episode index, e.g. "episode-003".

    Returns:
        {"episodeId", "found", "diagnoses": [...]} where each diagnosis has
        diagnosisId, verdict (MISSING_PERMISSION | BLOCKED_BY_GUARDRAIL |
        EXPLICIT_DENY | NOW_ALLOWED | AWS_MANAGED_ROLE | CANNOT_DIAGNOSE),
        principalArn, action, resource, explanation, fixVerified, occurrences,
        episodeIds, evidenceEventIds and limitations.
    """
    context = get_analysis_context()
    episode = context.episodes_by_id.get(episode_id)
    if episode is None:
        record_tool_call("diagnose_access_denied", {"episode_id": episode_id}, 0)
        return {"episodeId": episode_id, "found": False, "message": "No episode with that ID exists in this run.",
                "diagnoses": []}

    denied = [context.events_by_id[i] for i in episode.event_ids
              if i in context.events_by_id and is_access_denied(context.events_by_id[i])]
    groups: dict[tuple, list[NormalizedEvent]] = {}
    for event in denied:
        groups.setdefault(_group_key(event), []).append(event)

    episode_of = {eid: ep.episode_id for ep in context.episodes_by_id.values() for eid in ep.event_ids}
    results = []
    for key, group in list(groups.items())[:5]:
        with _LOCK:
            existing = context.access_diagnoses.get(key)
            if existing is None:
                existing = diagnose(group, context.events, f"AD-{len(context.access_diagnoses) + 1}", episode_of)
                context.access_diagnoses[key] = existing
                logger.info("diagnosis=%s verdict=%s action=%s occurrences=%d | access denied diagnosed",
                            existing.diagnosis_id, existing.verdict, existing.action, existing.occurrences)
        results.append(existing.model_dump(by_alias=True, mode="json"))

    record_tool_call("diagnose_access_denied", {"episode_id": episode_id}, len(results))
    if not results:
        return {"episodeId": episode_id, "found": True, "diagnoses": [],
                "message": "This episode has no AccessDenied or UnauthorizedOperation failures."}
    return {"episodeId": episode_id, "found": True, "diagnoses": results}
