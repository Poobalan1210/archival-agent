# Demo video: Archival Agent + diagnose_access_denied (target 2:40, hard limit 3:00)

**Setup**
- Have open in tabs:
  - `demo/out/production_2026-10-25.json`, or the email you received for Oct 25 (the BEFORE)
  - `demo/out/after.html` (the AFTER)
  - `agent/tools/access_diagnosis.py`
- Use a big font and a 1080p screen recording.

## 0:00–0:20 Hook (face or voice-over on the after report header)
"This is Archival Agent. Every morning it reads my AWS account's CloudTrail and emails me what changed and what failed.
It's run every day since July. This weekend I gave it one new tool, and it went from telling me *that* something broke to *why*, with a fix it has proved."

## 0:20–0:55 Before: the real report from the deployed agent
- Show the Oct 25 email/report. Scroll to the recommendation and the limitations.
- Read them out:
  > "Review the IAM policies for roles kyc90-harness-briefing and kyc90-harness-fraud…"
  > "The specific IAM policies that need to be updated… are **not identified**."
- "AccessDenied is the number one failure in my account: 2,347 in October alone. And all the agent could say was 'go look at IAM'.
  It also missed a third role entirely."

## 0:55–1:20 The tool
- Show `diagnose_access_denied` and its docstring, for about 5 seconds.
- "One Strands tool. For every denied call, it asks the **IAM policy simulator** why.
  When a permission is missing, it writes the smallest possible statement and **runs the simulator again** to prove it works.
  The model never writes the policy. Code and IAM do, and the validator rejects any diagnosis the model makes up."

## 1:20–2:15 After: same day, same model
- Show the scorecard from `python demo/before_after.py --before-report demo/out/production_2026-10-25.json`:
  root causes 0 → 3 · denied events explained 0 → 15 · fixes verified 0 → 3 · roles named 2/3 → 3/3 · gaps admitted 2 → 0.
- Open `after.html` and scroll to **"Access denied — explained"**:
  - 15 denied events → 3 root causes (one per role) → 3 fixes verified.
  - Zoom in on one card: `implicitDeny → with the proposed statement → allowed  [FIX VERIFIED]`.
  - Show the one-statement policy.
- Scroll to the recommendation: "Confirm whether these roles need ecr-public:GetAuthorizationToken. If they do, the minimal fix in AD-1… is verified."
- "It names all three roles, and it still leaves the decision to me, because a verified fix proves a call *would* be allowed, not that it *should* be."

## 2:15–2:40 Why it matters and how to reuse it
- "AWS built Diagnose with Amazon Q for one error at a time. This runs on its own, every day, across every denial, and proves each fix."
- "Reusable pattern: let an authoritative AWS engine produce the facts, and let the model explain them. One tool file and four read-only IAM permissions."

## 2:40 End card
Repo link + "Strands · Amazon Nova Pro · Bedrock AgentCore · IAM Policy Simulator".
