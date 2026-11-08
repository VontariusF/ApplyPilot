
# ApplyPilot agent-protocol dogfood transcript

Product head under test: `101a1ecddb2d4392db1b6ef9c8d62d71729de89b` (origin/main — the harness adds scripts only). Branch head: `101a1ecddb2d4392db1b6ef9c8d62d71729de89b`.

Run directory (throwaway): `/home/user/work/dogfood-pipeline` · fake claude: `/home/user/work/dogfood-pipeline/bin/claude` · LLM stub: `http://127.0.0.1:38363/v1` · Chromium: `/home/user/.cache/ms-playwright/chromium-1248/chrome-linux64/chrome`



Rig boundaries, disclosed up front (the deterministic stub-provider rig,

the same seams tests/test_agent_journey.py pins):



- Onboarding interview turns ride a scripted `claude -p` fake (print-mode

  contract pinned by tests/test_onboarding_agent.py).

- Scoring, tailoring, judging, and cover letters make real HTTP calls to a

  localhost OpenAI-compatible stub server (`LLM_PROVIDER=custom`).

- Discovery and enrichment are stubbed at their network seams; the stage

  code, DB writes, and scoring run for real.

- PDFs render through Playwright Chromium; the apply run drives the real

  launcher (Chrome worker + engine subprocess) with the fake engine

  emitting stream-json and the pinned RESULT verdicts.

- Every protocol verb below is the real CLI in a fresh invocation surface;


## 1 · Onboarding: intake → resume → kickoff → agent-led turn

```bash
$ applypilot agent start
```

```json
{
  "phase": "intake",
  "mode": "choices",
  "backend": "",
  "current_question": null,
  "prefill_chips": [],
  "last_agent_reply": null,
  "needs": {
    "resume": true,
    "confirm": false,
    "provider_setup": false
  },
  "progress": {
    "answered": 0,
    "remaining": 18
  },
  "resume": {
    "provided": false,
    "chars": null,
    "pending": false
  }
}
```

- PASS: start → phase intake

- PASS: start → needs.resume

```bash
$ applypilot agent next
```

```json
{
  "phase": "intake",
  "mode": "choices",
  "backend": "",
  "current_question": null,
  "prefill_chips": [],
  "last_agent_reply": null,
  "needs": {
    "resume": true,
    "confirm": false,
    "provider_setup": false
  },
  "progress": {
    "answered": 0,
    "remaining": 18
  },
  "resume": {
    "provided": false,
    "chars": null,
    "pending": false
  }
}
```

- PASS: next → phase intake, no question

```bash
$ applypilot agent resume --file /home/user/work/dogfood-pipeline/resume.txt
```

```json
{
  "phase": "intake",
  "mode": "choices",
  "backend": "",
  "current_question": null,
  "prefill_chips": [],
  "last_agent_reply": null,
  "needs": {
    "resume": false,
    "confirm": false,
    "provider_setup": false
  },
  "progress": {
    "answered": 0,
    "remaining": 18
  },
  "resume": {
    "provided": true,
    "chars": 133,
    "pending": true
  }
}
```

- PASS: resume → queued (pending)

- PASS: resume → needs.resume off

- PASS: resume block carries no text by default

```bash
$ applypilot agent start
```

```json
{
  "phase": "interview",
  "mode": "choices",
  "backend": "claude",
  "current_question": {
    "id": "full_name",
    "prompt": "What's your full name?",
    "choices": [],
    "prefilled": null,
    "allow_free_text": true,
    "free_text_kind": null
  },
  "prefill_chips": [],
  "last_agent_reply": "Hello! Tell me about your background.",
  "needs": {
    "resume": false,
    "confirm": false,
    "provider_setup": false
  },
  "progress": {
    "answered": 0,
    "remaining": 18
  },
  "resume": {
    "provided": true,
    "chars": 133,
    "pending": true
  }
}
```

- PASS: start → kickoff → interview

- PASS: start → mode choices

```bash
$ applypilot agent agent-led on
```

```json
{
  "phase": "interview",
  "mode": "agent",
  "backend": "claude",
  "current_question": {
    "id": "full_name",
    "prompt": "What's your full name?",
    "choices": [],
    "prefilled": null,
    "allow_free_text": true,
    "free_text_kind": null
  },
  "prefill_chips": [],
  "last_agent_reply": "Got it \u2014 I'll take it from here. What's your full name?",
  "needs": {
    "resume": false,
    "confirm": false,
    "provider_setup": false
  },
  "progress": {
    "answered": 0,
    "remaining": 18
  },
  "resume": {
    "provided": true,
    "chars": 133,
    "pending": true
  }
}
```

- PASS: agent-led on → mode agent

```bash
$ applypilot agent send --text "Lead with my data engineering background, please."
```

```json
{
  "phase": "interview",
  "mode": "agent",
  "backend": "claude",
  "current_question": {
    "id": "full_name",
    "prompt": "What's your full name?",
    "choices": [],
    "prefilled": null,
    "allow_free_text": true,
    "free_text_kind": null
  },
  "prefill_chips": [
    {
      "question_id": "work_style",
      "prompt": "How do you want to work?",
      "value": "remote",
      "choices": [
        {
          "value": "remote",
          "label": "Remote"
        },
        {
          "value": "hybrid",
          "label": "Hybrid"
        },
        {
          "value": "onsite",
          "label": "On-site"
        }
      ],
      "allow_free_text": false
    },
    {
      "question_id": "target_roles",
      "prompt": "Which job titles should we search for? One per line.",
      "value": "Data Engineer",
      "choices": [],
      "allow_free_text": true
    },
    {
      "question_id": "location",
      "prompt": "What city are you based in?",
      "value": "Austin, TX",
      "choices": [],
      "allow_free_text": true
    },
    {
      "question_id": "skills_languages",
      "prompt": "Which programming languages do you use?",
      "value": "Python, SQL",
      "choices": [],
      "allow_free_text": true
    }
  ],
  "last_agent_reply": "Got it \u2014 leading with your background.\n\nHere's what I have so far.\n\n[draft extracted \u2014 fenced text redacted; rerun with --reveal]\n",
  "needs": {
    "resume": false,
    "confirm": false,
    "provider_setup": false
  },
  "progress": {
    "answered": 0,
    "remaining": 15
  },
  "resume": {
    "provided": true,
    "chars": 133,
    "pending": false
  }
}
```

- PASS: send → agent turn lands

- PASS: send → partial draft redacted in the payload — Got it — leading with your background.

Here's what I have s

- PASS: redacted payload carries no resume text

```bash
$ applypilot agent next
```

```json
{
  "phase": "interview",
  "mode": "agent",
  "backend": "claude",
  "current_question": {
    "id": "full_name",
    "prompt": "What's your full name?",
    "choices": [],
    "prefilled": null,
    "allow_free_text": true,
    "free_text_kind": null
  },
  "prefill_chips": [
    {
      "question_id": "work_style",
      "prompt": "How do you want to work?",
      "value": "remote",
      "choices": [
        {
          "value": "remote",
          "label": "Remote"
        },
        {
          "value": "hybrid",
          "label": "Hybrid"
        },
        {
          "value": "onsite",
          "label": "On-site"
        }
      ],
      "allow_free_text": false
    },
    {
      "question_id": "target_roles",
      "prompt": "Which job titles should we search for? One per line.",
      "value": "Data Engineer",
      "choices": [],
      "allow_free_text": true
    },
    {
      "question_id": "location",
      "prompt": "What city are you based in?",
      "value": "Austin, TX",
      "choices": [],
      "allow_free_text": true
    },
    {
      "question_id": "skills_languages",
      "prompt": "Which programming languages do you use?",
      "value": "Python, SQL",
      "choices": [],
      "allow_free_text": true
    }
  ],
  "last_agent_reply": "Got it \u2014 leading with your background.\n\nHere's what I have so far.\n\n[draft extracted \u2014 fenced text redacted; rerun with --reveal]\n",
  "needs": {
    "resume": false,
    "confirm": false,
    "provider_setup": false
  },
  "progress": {
    "answered": 0,
    "remaining": 15
  },
  "resume": {
    "provided": true,
    "chars": 133,
    "pending": false
  }
}
```

- PASS: prefill bridge: chips carry the extracted draft — 4 chips

```bash
$ applypilot agent answer --question does-not-exist --choice x
```

```
(exit 1)
{
  "error": {
    "code": "unknown_question",
    "message": "unknown question",
    "details": null
  }
}
```

- PASS: refusal envelope code == unknown_question — got 'unknown_question'

```bash
$ applypilot agent agent-led off
```

```json
{
  "phase": "interview",
  "mode": "choices",
  "backend": "claude",
  "current_question": {
    "id": "full_name",
    "prompt": "What's your full name?",
    "choices": [],
    "prefilled": null,
    "allow_free_text": true,
    "free_text_kind": null
  },
  "prefill_chips": [
    {
      "question_id": "work_style",
      "prompt": "How do you want to work?",
      "value": "remote",
      "choices": [
        {
          "value": "remote",
          "label": "Remote"
        },
        {
          "value": "hybrid",
          "label": "Hybrid"
        },
        {
          "value": "onsite",
          "label": "On-site"
        }
      ],
      "allow_free_text": false
    },
    {
      "question_id": "target_roles",
      "prompt": "Which job titles should we search for? One per line.",
      "value": "Data Engineer",
      "choices": [],
      "allow_free_text": true
    },
    {
      "question_id": "location",
      "prompt": "What city are you based in?",
      "value": "Austin, TX",
      "choices": [],
      "allow_free_text": true
    },
    {
      "question_id": "skills_languages",
      "prompt": "Which programming languages do you use?",
      "value": "Python, SQL",
      "choices": [],
      "allow_free_text": true
    }
  ],
  "last_agent_reply": "Noted \u2014 tell me more when you're ready.",
  "needs": {
    "resume": false,
    "confirm": false,
    "provider_setup": false
  },
  "progress": {
    "answered": 0,
    "remaining": 15
  },
  "resume": {
    "provided": true,
    "chars": 133,
    "pending": false
  }
}
```

- PASS: agent-led off → mode choices


## 2 · Guided agenda: answer xN, payload-driven

```bash
$ applypilot agent next
```

```json
{"phase":"interview","mode":"choices","backend":"claude","current_question":{"id":"full_name","prompt":"What's your full name?","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":0,"remaining":15},"resume":{"provided":true,"chars":133,"pending":false}}
```

- answering `full_name` (free-text)

```bash
$ applypilot agent answer --question full_name --free-text "Something reasonable"
```

```json
{"phase":"interview","mode":"choices","backend":"claude","current_question":{"id":"education","prompt":"What's your highest education?","choices":[{"value":"Bachelor's","label":"Bachelor's"},{"value":"Master's","label":"Master's"},{"value":"PhD","label":"PhD"},{"value":"Self-taught","label":"Self-taught"}],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":1,"remaining":14},"resume":{"provided":true,"chars":133,"pending":false}}
```

```bash
$ applypilot agent next
```

```json
{"phase":"interview","mode":"choices","backend":"claude","current_question":{"id":"education","prompt":"What's your highest education?","choices":[{"value":"Bachelor's","label":"Bachelor's"},{"value":"Master's","label":"Master's"},{"value":"PhD","label":"PhD"},{"value":"Self-taught","label":"Self-taught"}],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":1,"remaining":14},"resume":{"provided":true,"chars":133,"pending":false}}
```

- answering `education` (choice="Bachelor's")

```bash
$ applypilot agent answer --question education --choice Bachelor's
```

```json
{"phase":"interview","mode":"choices","backend":"claude","current_question":{"id":"experience_years","prompt":"How many years have you worked in your field?","choices":[{"value":"0\u20132","label":"0\u20132"},{"value":"3\u20135","label":"3\u20135"},{"value":"6\u201310","label":"6\u201310"},{"value":"10+","label":"10+"}],"prefilled":null,"allow_free_text":false,"free_text_kind":null},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":2,"remaining":13},"resume":{"provided":true,"chars":133,"pending":false}}
```

```bash
$ applypilot agent next
```

```json
{"phase":"interview","mode":"choices","backend":"claude","current_question":{"id":"experience_years","prompt":"How many years have you worked in your field?","choices":[{"value":"0\u20132","label":"0\u20132"},{"value":"3\u20135","label":"3\u20135"},{"value":"6\u201310","label":"6\u201310"},{"value":"10+","label":"10+"}],"prefilled":null,"allow_free_text":false,"free_text_kind":null},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":2,"remaining":13},"resume":{"provided":true,"chars":133,"pending":false}}
```

- answering `experience_years` (choice='0–2')

```bash
$ applypilot agent answer --question experience_years --choice 0–2
```

```json
{"phase":"interview","mode":"choices","backend":"claude","current_question":{"id":"country","prompt":"Which country will you be working from / applying to jobs in?","choices":[{"value":"United States","label":"United States"},{"value":"United Kingdom","label":"United Kingdom"},{"value":"Canada","label":"Canada"},{"value":"European Union","label":"European Union"},{"value":"India","label":"India"},{"value":"Other","label":"Other"},{"value":"Not sure yet","label":"Not sure yet"}],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":3,"remaining":12},"resume":{"provided":true,"chars":133,"pending":false}}
```

```bash
$ applypilot agent next
```

```json
{"phase":"interview","mode":"choices","backend":"claude","current_question":{"id":"country","prompt":"Which country will you be working from / applying to jobs in?","choices":[{"value":"United States","label":"United States"},{"value":"United Kingdom","label":"United Kingdom"},{"value":"Canada","label":"Canada"},{"value":"European Union","label":"European Union"},{"value":"India","label":"India"},{"value":"Other","label":"Other"},{"value":"Not sure yet","label":"Not sure yet"}],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":3,"remaining":12},"resume":{"provided":true,"chars":133,"pending":false}}
```

- answering `country` (choice='United States')

```bash
$ applypilot agent answer --question country --choice "United States"
```

```json
{"phase":"interview","mode":"choices","backend":"claude","current_question":{"id":"work_authorized","prompt":"Are you legally authorized to work in the country you're applying in?","choices":[{"value":"yes","label":"Yes"},{"value":"no","label":"No"}],"prefilled":null,"allow_free_text":false,"free_text_kind":null},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":4,"remaining":14},"resume":{"provided":true,"chars":133,"pending":false}}
```

```bash
$ applypilot agent next
```

```json
{"phase":"interview","mode":"choices","backend":"claude","current_question":{"id":"work_authorized","prompt":"Are you legally authorized to work in the country you're applying in?","choices":[{"value":"yes","label":"Yes"},{"value":"no","label":"No"}],"prefilled":null,"allow_free_text":false,"free_text_kind":null},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":4,"remaining":14},"resume":{"provided":true,"chars":133,"pending":false}}
```

- answering `work_authorized` (choice='yes')

```bash
$ applypilot agent answer --question work_authorized --choice yes
```

```json
{"phase":"interview","mode":"choices","backend":"claude","current_question":{"id":"sponsorship","prompt":"Will you now or in the future need visa sponsorship?","choices":[{"value":"yes","label":"Yes"},{"value":"no","label":"No"},{"value":"not_sure","label":"Not sure"}],"prefilled":null,"allow_free_text":false,"free_text_kind":null},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":5,"remaining":14},"resume":{"provided":true,"chars":133,"pending":false}}
```

```bash
$ applypilot agent next
```

```json
{"phase":"interview","mode":"choices","backend":"claude","current_question":{"id":"sponsorship","prompt":"Will you now or in the future need visa sponsorship?","choices":[{"value":"yes","label":"Yes"},{"value":"no","label":"No"},{"value":"not_sure","label":"Not sure"}],"prefilled":null,"allow_free_text":false,"free_text_kind":null},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":5,"remaining":14},"resume":{"provided":true,"chars":133,"pending":false}}
```

- answering `sponsorship` (choice='yes')

```bash
$ applypilot agent answer --question sponsorship --choice yes
```

```json
{"phase":"interview","mode":"choices","backend":"claude","current_question":{"id":"permit_type","prompt":"Which best describes your right to work?","choices":[{"value":"Citizen","label":"Citizen"},{"value":"Permanent resident","label":"Permanent resident"},{"value":"Open work permit","label":"Open work permit"},{"value":"H-1B","label":"H-1B"}],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":6,"remaining":13},"resume":{"provided":true,"chars":133,"pending":false}}
```

```bash
$ applypilot agent next
```

```json
{"phase":"interview","mode":"choices","backend":"claude","current_question":{"id":"permit_type","prompt":"Which best describes your right to work?","choices":[{"value":"Citizen","label":"Citizen"},{"value":"Permanent resident","label":"Permanent resident"},{"value":"Open work permit","label":"Open work permit"},{"value":"H-1B","label":"H-1B"}],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":6,"remaining":13},"resume":{"provided":true,"chars":133,"pending":false}}
```

- answering `permit_type` (choice='Citizen')

```bash
$ applypilot agent answer --question permit_type --choice Citizen
```

```json
{"phase":"interview","mode":"choices","backend":"claude","current_question":{"id":"currency","prompt":"Which currency should salary conversations use?","choices":[{"value":"USD","label":"USD"},{"value":"EUR","label":"EUR"},{"value":"GBP","label":"GBP"},{"value":"CAD","label":"CAD"},{"value":"INR","label":"INR"}],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":7,"remaining":12},"resume":{"provided":true,"chars":133,"pending":false}}
```

```bash
$ applypilot agent next
```

```json
{"phase":"interview","mode":"choices","backend":"claude","current_question":{"id":"currency","prompt":"Which currency should salary conversations use?","choices":[{"value":"USD","label":"USD"},{"value":"EUR","label":"EUR"},{"value":"GBP","label":"GBP"},{"value":"CAD","label":"CAD"},{"value":"INR","label":"INR"}],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":7,"remaining":12},"resume":{"provided":true,"chars":133,"pending":false}}
```

- answering `currency` (choice='USD')

```bash
$ applypilot agent answer --question currency --choice USD
```

```json
{"phase":"interview","mode":"choices","backend":"claude","current_question":{"id":"salary_band","prompt":"What's the minimum annual salary you'd accept?","choices":[{"value":"60000","label":"$60k \u2013 $80k"},{"value":"80000","label":"$80k \u2013 $100k"},{"value":"100000","label":"$100k \u2013 $120k"},{"value":"120000","label":"$120k+"}],"prefilled":null,"allow_free_text":true,"free_text_kind":"number"},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":8,"remaining":11},"resume":{"provided":true,"chars":133,"pending":false}}
```

```bash
$ applypilot agent next
```

```json
{"phase":"interview","mode":"choices","backend":"claude","current_question":{"id":"salary_band","prompt":"What's the minimum annual salary you'd accept?","choices":[{"value":"60000","label":"$60k \u2013 $80k"},{"value":"80000","label":"$80k \u2013 $100k"},{"value":"100000","label":"$100k \u2013 $120k"},{"value":"120000","label":"$120k+"}],"prefilled":null,"allow_free_text":true,"free_text_kind":"number"},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":8,"remaining":11},"resume":{"provided":true,"chars":133,"pending":false}}
```

- answering `salary_band` (choice='60000')

```bash
$ applypilot agent answer --question salary_band --choice 60000
```

```json
{"phase":"interview","mode":"choices","backend":"claude","current_question":{"id":"start_date","prompt":"When could you start?","choices":[{"value":"Immediately","label":"Immediately"},{"value":"In 2 weeks","label":"In 2 weeks"},{"value":"In 1 month","label":"In 1 month"},{"value":"In 3 months","label":"In 3 months"}],"prefilled":null,"allow_free_text":true,"free_text_kind":"date"},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":9,"remaining":10},"resume":{"provided":true,"chars":133,"pending":false}}
```

```bash
$ applypilot agent next
```

```json
{"phase":"interview","mode":"choices","backend":"claude","current_question":{"id":"start_date","prompt":"When could you start?","choices":[{"value":"Immediately","label":"Immediately"},{"value":"In 2 weeks","label":"In 2 weeks"},{"value":"In 1 month","label":"In 1 month"},{"value":"In 3 months","label":"In 3 months"}],"prefilled":null,"allow_free_text":true,"free_text_kind":"date"},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":9,"remaining":10},"resume":{"provided":true,"chars":133,"pending":false}}
```

- answering `start_date` (choice='Immediately')

```bash
$ applypilot agent answer --question start_date --choice Immediately
```

```json
{"phase":"interview","mode":"choices","backend":"claude","current_question":{"id":"skills_frameworks","prompt":"Which frameworks or libraries do you use?","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":"multiline"},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":10,"remaining":9},"resume":{"provided":true,"chars":133,"pending":false}}
```

```bash
$ applypilot agent next
```

```json
{"phase":"interview","mode":"choices","backend":"claude","current_question":{"id":"skills_frameworks","prompt":"Which frameworks or libraries do you use?","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":"multiline"},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":10,"remaining":9},"resume":{"provided":true,"chars":133,"pending":false}}
```

- answering `skills_frameworks` (free-text)

```bash
$ applypilot agent answer --question skills_frameworks --free-text "Something reasonable"
```

```json
{"phase":"interview","mode":"choices","backend":"claude","current_question":{"id":"skills_tools","prompt":"Which tools do you work with?","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":"multiline"},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":11,"remaining":8},"resume":{"provided":true,"chars":133,"pending":false}}
```

```bash
$ applypilot agent next
```

```json
{"phase":"interview","mode":"choices","backend":"claude","current_question":{"id":"skills_tools","prompt":"Which tools do you work with?","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":"multiline"},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":11,"remaining":8},"resume":{"provided":true,"chars":133,"pending":false}}
```

- answering `skills_tools` (free-text)

```bash
$ applypilot agent answer --question skills_tools --free-text "Something reasonable"
```

```json
{"phase":"interview","mode":"choices","backend":"claude","current_question":{"id":"email","prompt":"What email address should applications use?","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":12,"remaining":7},"resume":{"provided":true,"chars":133,"pending":false}}
```

```bash
$ applypilot agent next
```

```json
{"phase":"interview","mode":"choices","backend":"claude","current_question":{"id":"email","prompt":"What email address should applications use?","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":12,"remaining":7},"resume":{"provided":true,"chars":133,"pending":false}}
```

- answering `email` (free-text)

```bash
$ applypilot agent answer --question email --free-text "Something reasonable"
```

```json
{"phase":"interview","mode":"choices","backend":"claude","current_question":{"id":"phone","prompt":"What phone number should applications use?","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":13,"remaining":6},"resume":{"provided":true,"chars":133,"pending":false}}
```

```bash
$ applypilot agent next
```

```json
{"phase":"interview","mode":"choices","backend":"claude","current_question":{"id":"phone","prompt":"What phone number should applications use?","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":13,"remaining":6},"resume":{"provided":true,"chars":133,"pending":false}}
```

- answering `phone` (free-text)

```bash
$ applypilot agent answer --question phone --free-text "Something reasonable"
```

```json
{"phase":"interview","mode":"choices","backend":"claude","current_question":{"id":"city","prompt":"What city do you live in?","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":14,"remaining":5},"resume":{"provided":true,"chars":133,"pending":false}}
```

```bash
$ applypilot agent next
```

```json
{"phase":"interview","mode":"choices","backend":"claude","current_question":{"id":"city","prompt":"What city do you live in?","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":14,"remaining":5},"resume":{"provided":true,"chars":133,"pending":false}}
```

- answering `city` (free-text)

```bash
$ applypilot agent answer --question city --free-text "Something reasonable"
```

```json
{"phase":"interview","mode":"choices","backend":"claude","current_question":{"id":"linkedin_url","prompt":"Link your LinkedIn profile (optional)","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":15,"remaining":4},"resume":{"provided":true,"chars":133,"pending":false}}
```

```bash
$ applypilot agent next
```

```json
{"phase":"interview","mode":"choices","backend":"claude","current_question":{"id":"linkedin_url","prompt":"Link your LinkedIn profile (optional)","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":15,"remaining":4},"resume":{"provided":true,"chars":133,"pending":false}}
```

- answering `linkedin_url` (free-text)

```bash
$ applypilot agent answer --question linkedin_url --free-text "Something reasonable"
```

```json
{"phase":"interview","mode":"choices","backend":"claude","current_question":{"id":"github_url","prompt":"Link your GitHub profile (optional)","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":16,"remaining":3},"resume":{"provided":true,"chars":133,"pending":false}}
```

```bash
$ applypilot agent next
```

```json
{"phase":"interview","mode":"choices","backend":"claude","current_question":{"id":"github_url","prompt":"Link your GitHub profile (optional)","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":16,"remaining":3},"resume":{"provided":true,"chars":133,"pending":false}}
```

- answering `github_url` (free-text)

```bash
$ applypilot agent answer --question github_url --free-text "Something reasonable"
```

```json
{"phase":"interview","mode":"choices","backend":"claude","current_question":{"id":"website_url","prompt":"Link a personal website (optional)","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":17,"remaining":2},"resume":{"provided":true,"chars":133,"pending":false}}
```

```bash
$ applypilot agent next
```

```json
{"phase":"interview","mode":"choices","backend":"claude","current_question":{"id":"website_url","prompt":"Link a personal website (optional)","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":17,"remaining":2},"resume":{"provided":true,"chars":133,"pending":false}}
```

- answering `website_url` (free-text)

```bash
$ applypilot agent answer --question website_url --free-text "Something reasonable"
```

```json
{"phase":"interview","mode":"choices","backend":"claude","current_question":{"id":"portfolio_url","prompt":"Link a portfolio site (optional)","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":18,"remaining":1},"resume":{"provided":true,"chars":133,"pending":false}}
```

```bash
$ applypilot agent next
```

```json
{"phase":"interview","mode":"choices","backend":"claude","current_question":{"id":"portfolio_url","prompt":"Link a portfolio site (optional)","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":18,"remaining":1},"resume":{"provided":true,"chars":133,"pending":false}}
```

- answering `portfolio_url` (free-text)

```bash
$ applypilot agent answer --question portfolio_url --free-text "Something reasonable"
```

```json
{"phase":"review","mode":"choices","backend":"claude","current_question":null,"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":true,"provider_setup":false},"progress":{"answered":19,"remaining":0},"resume":{"provided":true,"chars":133,"pending":false},"review_fields":[{"id":"full_name","prompt":"What's your full name?","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"education","prompt":"What's your highest education?","value":"Bachelor's","choices":[{"value":"Bachelor's","label":"Bachelor's"},{"value":"Master's","label":"Master's"},{"value":"PhD","label":"PhD"},{"value":"Self-taught","label":"Self-taught"}],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"experience_years","prompt":"How many years have you worked in your field?","value":"0\u20132","choices":[{"value":"0\u20132","label":"0\u20132"},{"value":"3\u20135","label":"3\u20135"},{"value":"6\u201310","label":"6\u201310"},{"value":"10+","label":"10+"}],"allow_free_text":false,"from_resume":false,"needs_answer":false},{"id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false,"from_resume":true,"needs_answer":false},{"id":"country","prompt":"Which country will you be working from / applying to jobs in?","value":"United States","choices":[{"value":"United States","label":"United States"},{"value":"United Kingdom","label":"United Kingdom"},{"value":"Canada","label":"Canada"},{"value":"European Union","label":"European Union"},{"value":"India","label":"India"},{"value":"Other","label":"Other"},{"value":"Not sure yet","label":"Not sure yet"}],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"work_authorized","prompt":"Are you legally authorized to work in the country you're applying in?","value":"yes","choices":[{"value":"yes","label":"Yes"},{"value":"no","label":"No"}],"allow_free_text":false,"from_resume":false,"needs_answer":false},{"id":"sponsorship","prompt":"Will you now or in the future need visa sponsorship?","value":"yes","choices":[{"value":"yes","label":"Yes"},{"value":"no","label":"No"},{"value":"not_sure","label":"Not sure"}],"allow_free_text":false,"from_resume":false,"needs_answer":false},{"id":"permit_type","prompt":"Which best describes your right to work?","value":"Citizen","choices":[{"value":"Citizen","label":"Citizen"},{"value":"Permanent resident","label":"Permanent resident"},{"value":"Open work permit","label":"Open work permit"},{"value":"H-1B","label":"H-1B"}],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"currency","prompt":"Which currency should salary conversations use?","value":"USD","choices":[{"value":"USD","label":"USD"},{"value":"EUR","label":"EUR"},{"value":"GBP","label":"GBP"},{"value":"CAD","label":"CAD"},{"value":"INR","label":"INR"}],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"salary_band","prompt":"What's the minimum annual salary you'd accept?","value":"60000","choices":[{"value":"60000","label":"$60k \u2013 $80k"},{"value":"80000","label":"$80k \u2013 $100k"},{"value":"100000","label":"$100k \u2013 $120k"},{"value":"120000","label":"$120k+"}],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"start_date","prompt":"When could you start?","value":"Immediately","choices":[{"value":"Immediately","label":"Immediately"},{"value":"In 2 weeks","label":"In 2 weeks"},{"value":"In 1 month","label":"In 1 month"},{"value":"In 3 months","label":"In 3 months"}],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true,"from_resume":true,"needs_answer":false},{"id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true,"from_resume":true,"needs_answer":false},{"id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true,"from_resume":true,"needs_answer":false},{"id":"skills_frameworks","prompt":"Which frameworks or libraries do you use?","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"skills_tools","prompt":"Which tools do you work with?","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"email","prompt":"What email address should applications use?","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"phone","prompt":"What phone number should applications use?","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"city","prompt":"What city do you live in?","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"linkedin_url","prompt":"Link your LinkedIn profile (optional)","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"github_url","prompt":"Link your GitHub profile (optional)","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"website_url","prompt":"Link a personal website (optional)","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"portfolio_url","prompt":"Link a portfolio site (optional)","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false}],"missing_review_fields":[]}
```

```bash
$ applypilot agent next
```

```json
{"phase":"review","mode":"choices","backend":"claude","current_question":null,"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":true,"provider_setup":false},"progress":{"answered":19,"remaining":0},"resume":{"provided":true,"chars":133,"pending":false},"review_fields":[{"id":"full_name","prompt":"What's your full name?","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"education","prompt":"What's your highest education?","value":"Bachelor's","choices":[{"value":"Bachelor's","label":"Bachelor's"},{"value":"Master's","label":"Master's"},{"value":"PhD","label":"PhD"},{"value":"Self-taught","label":"Self-taught"}],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"experience_years","prompt":"How many years have you worked in your field?","value":"0\u20132","choices":[{"value":"0\u20132","label":"0\u20132"},{"value":"3\u20135","label":"3\u20135"},{"value":"6\u201310","label":"6\u201310"},{"value":"10+","label":"10+"}],"allow_free_text":false,"from_resume":false,"needs_answer":false},{"id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false,"from_resume":true,"needs_answer":false},{"id":"country","prompt":"Which country will you be working from / applying to jobs in?","value":"United States","choices":[{"value":"United States","label":"United States"},{"value":"United Kingdom","label":"United Kingdom"},{"value":"Canada","label":"Canada"},{"value":"European Union","label":"European Union"},{"value":"India","label":"India"},{"value":"Other","label":"Other"},{"value":"Not sure yet","label":"Not sure yet"}],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"work_authorized","prompt":"Are you legally authorized to work in the country you're applying in?","value":"yes","choices":[{"value":"yes","label":"Yes"},{"value":"no","label":"No"}],"allow_free_text":false,"from_resume":false,"needs_answer":false},{"id":"sponsorship","prompt":"Will you now or in the future need visa sponsorship?","value":"yes","choices":[{"value":"yes","label":"Yes"},{"value":"no","label":"No"},{"value":"not_sure","label":"Not sure"}],"allow_free_text":false,"from_resume":false,"needs_answer":false},{"id":"permit_type","prompt":"Which best describes your right to work?","value":"Citizen","choices":[{"value":"Citizen","label":"Citizen"},{"value":"Permanent resident","label":"Permanent resident"},{"value":"Open work permit","label":"Open work permit"},{"value":"H-1B","label":"H-1B"}],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"currency","prompt":"Which currency should salary conversations use?","value":"USD","choices":[{"value":"USD","label":"USD"},{"value":"EUR","label":"EUR"},{"value":"GBP","label":"GBP"},{"value":"CAD","label":"CAD"},{"value":"INR","label":"INR"}],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"salary_band","prompt":"What's the minimum annual salary you'd accept?","value":"60000","choices":[{"value":"60000","label":"$60k \u2013 $80k"},{"value":"80000","label":"$80k \u2013 $100k"},{"value":"100000","label":"$100k \u2013 $120k"},{"value":"120000","label":"$120k+"}],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"start_date","prompt":"When could you start?","value":"Immediately","choices":[{"value":"Immediately","label":"Immediately"},{"value":"In 2 weeks","label":"In 2 weeks"},{"value":"In 1 month","label":"In 1 month"},{"value":"In 3 months","label":"In 3 months"}],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true,"from_resume":true,"needs_answer":false},{"id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true,"from_resume":true,"needs_answer":false},{"id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true,"from_resume":true,"needs_answer":false},{"id":"skills_frameworks","prompt":"Which frameworks or libraries do you use?","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"skills_tools","prompt":"Which tools do you work with?","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"email","prompt":"What email address should applications use?","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"phone","prompt":"What phone number should applications use?","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"city","prompt":"What city do you live in?","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"linkedin_url","prompt":"Link your LinkedIn profile (optional)","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"github_url","prompt":"Link your GitHub profile (optional)","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"website_url","prompt":"Link a personal website (optional)","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"portfolio_url","prompt":"Link a portfolio site (optional)","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false}],"missing_review_fields":[]}
```

- PASS: agenda drained to review — 19 answers


### 3 · Review: fields, edits, keep-editing, back to review, confirm

```bash
$ applypilot agent next
```

```json
{
  "phase": "review",
  "mode": "choices",
  "backend": "claude",
  "current_question": null,
  "prefill_chips": [
    {
      "question_id": "work_style",
      "prompt": "How do you want to work?",
      "value": "remote",
      "choices": [
        {
          "value": "remote",
          "label": "Remote"
        },
        {
          "value": "hybrid",
          "label": "Hybrid"
        },
        {
          "value": "onsite",
          "label": "On-site"
        }
      ],
      "allow_free_text": false
    },
    {
      "question_id": "target_roles",
      "prompt": "Which job titles should we search for? One per line.",
      "value": "Data Engineer",
      "choices": [],
      "allow_free_text": true
    },
    {
      "question_id": "location",
      "prompt": "What city are you based in?",
      "value": "Austin, TX",
      "choices": [],
      "allow_free_text": true
    },
    {
      "question_id": "skills_languages",
      "prompt": "Which programming languages do you use?",
      "value": "Python, SQL",
      "choices": [],
      "allow_free_text": true
    }
  ],
  "last_agent_reply": "Noted \u2014 tell me more when you're ready.",
  "needs": {
    "resume": false,
    "confirm": true,
    "provider_setup": false
  },
  "progress": {
    "answered": 19,
    "remaining": 0
  },
  "resume": {
    "provided": true,
    "chars": 133,
    "pending": false
  },
  "review_fields": [
    {
      "id": "full_name",
      "prompt": "What's your full name?",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "education",
      "prompt": "What's your highest education?",
      "value": "Bachelor's",
      "choices": [
        {
          "value": "Bachelor's",
          "label": "Bachelor's"
        },
        {
          "value": "Master's",
          "label": "Master's"
        },
        {
          "value": "PhD",
          "label": "PhD"
        },
        {
          "value": "Self-taught",
          "label": "Self-taught"
        }
      ],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "experience_years",
      "prompt": "How many years have you worked in your field?",
      "value": "0\u20132",
      "choices": [
        {
          "value": "0\u20132",
          "label": "0\u20132"
        },
        {
          "value": "3\u20135",
          "label": "3\u20135"
        },
        {
          "value": "6\u201310",
          "label": "6\u201310"
        },
        {
          "value": "10+",
          "label": "10+"
        }
      ],
      "allow_free_text": false,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "work_style",
      "prompt": "How do you want to work?",
      "value": "remote",
      "choices": [
        {
          "value": "remote",
          "label": "Remote"
        },
        {
          "value": "hybrid",
          "label": "Hybrid"
        },
        {
          "value": "onsite",
          "label": "On-site"
        }
      ],
      "allow_free_text": false,
      "from_resume": true,
      "needs_answer": false
    },
    {
      "id": "country",
      "prompt": "Which country will you be working from / applying to jobs in?",
      "value": "United States",
      "choices": [
        {
          "value": "United States",
          "label": "United States"
        },
        {
          "value": "United Kingdom",
          "label": "United Kingdom"
        },
        {
          "value": "Canada",
          "label": "Canada"
        },
        {
          "value": "European Union",
          "label": "European Union"
        },
        {
          "value": "India",
          "label": "India"
        },
        {
          "value": "Other",
          "label": "Other"
        },
        {
          "value": "Not sure yet",
          "label": "Not sure yet"
        }
      ],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "work_authorized",
      "prompt": "Are you legally authorized to work in the country you're applying in?",
      "value": "yes",
      "choices": [
        {
          "value": "yes",
          "label": "Yes"
        },
        {
          "value": "no",
          "label": "No"
        }
      ],
      "allow_free_text": false,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "sponsorship",
      "prompt": "Will you now or in the future need visa sponsorship?",
      "value": "yes",
      "choices": [
        {
          "value": "yes",
          "label": "Yes"
        },
        {
          "value": "no",
          "label": "No"
        },
        {
          "value": "not_sure",
          "label": "Not sure"
        }
      ],
      "allow_free_text": false,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "permit_type",
      "prompt": "Which best describes your right to work?",
      "value": "Citizen",
      "choices": [
        {
          "value": "Citizen",
          "label": "Citizen"
        },
        {
          "value": "Permanent resident",
          "label": "Permanent resident"
        },
        {
          "value": "Open work permit",
          "label": "Open work permit"
        },
        {
          "value": "H-1B",
          "label": "H-1B"
        }
      ],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "currency",
      "prompt": "Which currency should salary conversations use?",
      "value": "USD",
      "choices": [
        {
          "value": "USD",
          "label": "USD"
        },
        {
          "value": "EUR",
          "label": "EUR"
        },
        {
          "value": "GBP",
          "label": "GBP"
        },
        {
          "value": "CAD",
          "label": "CAD"
        },
        {
          "value": "INR",
          "label": "INR"
        }
      ],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "salary_band",
      "prompt": "What's the minimum annual salary you'd accept?",
      "value": "60000",
      "choices": [
        {
          "value": "60000",
          "label": "$60k \u2013 $80k"
        },
        {
          "value": "80000",
          "label": "$80k \u2013 $100k"
        },
        {
          "value": "100000",
          "label": "$100k \u2013 $120k"
        },
        {
          "value": "120000",
          "label": "$120k+"
        }
      ],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "start_date",
      "prompt": "When could you start?",
      "value": "Immediately",
      "choices": [
        {
          "value": "Immediately",
          "label": "Immediately"
        },
        {
          "value": "In 2 weeks",
          "label": "In 2 weeks"
        },
        {
          "value": "In 1 month",
          "label": "In 1 month"
        },
        {
          "value": "In 3 months",
          "label": "In 3 months"
        }
      ],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "target_roles",
      "prompt": "Which job titles should we search for? One per line.",
      "value": "Data Engineer",
      "choices": [],
      "allow_free_text": true,
      "from_resume": true,
      "needs_answer": false
    },
    {
      "id": "location",
      "prompt": "What city are you based in?",
      "value": "Austin, TX",
      "choices": [],
      "allow_free_text": true,
      "from_resume": true,
      "needs_answer": false
    },
    {
      "id": "skills_languages",
      "prompt": "Which programming languages do you use?",
      "value": "Python, SQL",
      "choices": [],
      "allow_free_text": true,
      "from_resume": true,
      "needs_answer": false
    },
    {
      "id": "skills_frameworks",
      "prompt": "Which frameworks or libraries do you use?",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "skills_tools",
      "prompt": "Which tools do you work with?",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "email",
      "prompt": "What email address should applications use?",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "phone",
      "prompt": "What phone number should applications use?",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "city",
      "prompt": "What city do you live in?",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "linkedin_url",
      "prompt": "Link your LinkedIn profile (optional)",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "github_url",
      "prompt": "Link your GitHub profile (optional)",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "website_url",
      "prompt": "Link a personal website (optional)",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "portfolio_url",
      "prompt": "Link a portfolio site (optional)",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    }
  ],
  "missing_review_fields": []
}
```

- PASS: next → phase review

- PASS: next → needs.confirm

- PASS: review fields ride the payload

```bash
$ applypilot agent next --reveal
```

```json
{
  "phase": "review",
  "mode": "choices",
  "backend": "claude",
  "current_question": null,
  "prefill_chips": [
    {
      "question_id": "work_style",
      "prompt": "How do you want to work?",
      "value": "remote",
      "choices": [
        {
          "value": "remote",
          "label": "Remote"
        },
        {
          "value": "hybrid",
          "label": "Hybrid"
        },
        {
          "value": "onsite",
          "label": "On-site"
        }
      ],
      "allow_free_text": false
    },
    {
      "question_id": "target_roles",
      "prompt": "Which job titles should we search for? One per line.",
      "value": "Data Engineer",
      "choices": [],
      "allow_free_text": true
    },
    {
      "question_id": "location",
      "prompt": "What city are you based in?",
      "value": "Austin, TX",
      "choices": [],
      "allow_free_text": true
    },
    {
      "question_id": "skills_languages",
      "prompt": "Which programming languages do you use?",
      "value": "Python, SQL",
      "choices": [],
      "allow_free_text": true
    }
  ],
  "last_agent_reply": "Noted \u2014 tell me more when you're ready.",
  "needs": {
    "resume": false,
    "confirm": true,
    "provider_setup": false
  },
  "progress": {
    "answered": 19,
    "remaining": 0
  },
  "resume": {
    "provided": true,
    "chars": 133,
    "pending": false,
    "text": "Test Person \u2014 data engineer resume.\nSkills: Python, SQL, Airflow.\nExperience: built analytics pipelines at Example Co, 2020-present.\n"
  },
  "review_fields": [
    {
      "id": "full_name",
      "prompt": "What's your full name?",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "education",
      "prompt": "What's your highest education?",
      "value": "Bachelor's",
      "choices": [
        {
          "value": "Bachelor's",
          "label": "Bachelor's"
        },
        {
          "value": "Master's",
          "label": "Master's"
        },
        {
          "value": "PhD",
          "label": "PhD"
        },
        {
          "value": "Self-taught",
          "label": "Self-taught"
        }
      ],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "experience_years",
      "prompt": "How many years have you worked in your field?",
      "value": "0\u20132",
      "choices": [
        {
          "value": "0\u20132",
          "label": "0\u20132"
        },
        {
          "value": "3\u20135",
          "label": "3\u20135"
        },
        {
          "value": "6\u201310",
          "label": "6\u201310"
        },
        {
          "value": "10+",
          "label": "10+"
        }
      ],
      "allow_free_text": false,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "work_style",
      "prompt": "How do you want to work?",
      "value": "remote",
      "choices": [
        {
          "value": "remote",
          "label": "Remote"
        },
        {
          "value": "hybrid",
          "label": "Hybrid"
        },
        {
          "value": "onsite",
          "label": "On-site"
        }
      ],
      "allow_free_text": false,
      "from_resume": true,
      "needs_answer": false
    },
    {
      "id": "country",
      "prompt": "Which country will you be working from / applying to jobs in?",
      "value": "United States",
      "choices": [
        {
          "value": "United States",
          "label": "United States"
        },
        {
          "value": "United Kingdom",
          "label": "United Kingdom"
        },
        {
          "value": "Canada",
          "label": "Canada"
        },
        {
          "value": "European Union",
          "label": "European Union"
        },
        {
          "value": "India",
          "label": "India"
        },
        {
          "value": "Other",
          "label": "Other"
        },
        {
          "value": "Not sure yet",
          "label": "Not sure yet"
        }
      ],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "work_authorized",
      "prompt": "Are you legally authorized to work in the country you're applying in?",
      "value": "yes",
      "choices": [
        {
          "value": "yes",
          "label": "Yes"
        },
        {
          "value": "no",
          "label": "No"
        }
      ],
      "allow_free_text": false,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "sponsorship",
      "prompt": "Will you now or in the future need visa sponsorship?",
      "value": "yes",
      "choices": [
        {
          "value": "yes",
          "label": "Yes"
        },
        {
          "value": "no",
          "label": "No"
        },
        {
          "value": "not_sure",
          "label": "Not sure"
        }
      ],
      "allow_free_text": false,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "permit_type",
      "prompt": "Which best describes your right to work?",
      "value": "Citizen",
      "choices": [
        {
          "value": "Citizen",
          "label": "Citizen"
        },
        {
          "value": "Permanent resident",
          "label": "Permanent resident"
        },
        {
          "value": "Open work permit",
          "label": "Open work permit"
        },
        {
          "value": "H-1B",
          "label": "H-1B"
        }
      ],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "currency",
      "prompt": "Which currency should salary conversations use?",
      "value": "USD",
      "choices": [
        {
          "value": "USD",
          "label": "USD"
        },
        {
          "value": "EUR",
          "label": "EUR"
        },
        {
          "value": "GBP",
          "label": "GBP"
        },
        {
          "value": "CAD",
          "label": "CAD"
        },
        {
          "value": "INR",
          "label": "INR"
        }
      ],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "salary_band",
      "prompt": "What's the minimum annual salary you'd accept?",
      "value": "60000",
      "choices": [
        {
          "value": "60000",
          "label": "$60k \u2013 $80k"
        },
        {
          "value": "80000",
          "label": "$80k \u2013 $100k"
        },
        {
          "value": "100000",
          "label": "$100k \u2013 $120k"
        },
        {
          "value": "120000",
          "label": "$120k+"
        }
      ],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "start_date",
      "prompt": "When could you start?",
      "value": "Immediately",
      "choices": [
        {
          "value": "Immediately",
          "label": "Immediately"
        },
        {
          "value": "In 2 weeks",
          "label": "In 2 weeks"
        },
        {
          "value": "In 1 month",
          "label": "In 1 month"
        },
        {
          "value": "In 3 months",
          "label": "In 3 months"
        }
      ],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "target_roles",
      "prompt": "Which job titles should we search for? One per line.",
      "value": "Data Engineer",
      "choices": [],
      "allow_free_text": true,
      "from_resume": true,
      "needs_answer": false
    },
    {
      "id": "location",
      "prompt": "What city are you based in?",
      "value": "Austin, TX",
      "choices": [],
      "allow_free_text": true,
      "from_resume": true,
      "needs_answer": false
    },
    {
      "id": "skills_languages",
      "prompt": "Which programming languages do you use?",
      "value": "Python, SQL",
      "choices": [],
      "allow_free_text": true,
      "from_resume": true,
      "needs_answer": false
    },
    {
      "id": "skills_frameworks",
      "prompt": "Which frameworks or libraries do you use?",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "skills_tools",
      "prompt": "Which tools do you work with?",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "email",
      "prompt": "What email address should applications use?",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "phone",
      "prompt": "What phone number should applications use?",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "city",
      "prompt": "What city do you live in?",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "linkedin_url",
      "prompt": "Link your LinkedIn profile (optional)",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "github_url",
      "prompt": "Link your GitHub profile (optional)",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "website_url",
      "prompt": "Link a personal website (optional)",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "portfolio_url",
      "prompt": "Link a portfolio site (optional)",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    }
  ],
  "missing_review_fields": [],
  "resume_facts": {
    "preserved_companies": [],
    "preserved_projects": [],
    "preserved_school": "",
    "real_metrics": []
  }
}
```

- PASS: --reveal surfaces resume_facts

```bash
$ applypilot agent review --set phone=555-0199
```

```json
{
  "phase": "review",
  "mode": "choices",
  "backend": "claude",
  "current_question": null,
  "prefill_chips": [
    {
      "question_id": "work_style",
      "prompt": "How do you want to work?",
      "value": "remote",
      "choices": [
        {
          "value": "remote",
          "label": "Remote"
        },
        {
          "value": "hybrid",
          "label": "Hybrid"
        },
        {
          "value": "onsite",
          "label": "On-site"
        }
      ],
      "allow_free_text": false
    },
    {
      "question_id": "target_roles",
      "prompt": "Which job titles should we search for? One per line.",
      "value": "Data Engineer",
      "choices": [],
      "allow_free_text": true
    },
    {
      "question_id": "location",
      "prompt": "What city are you based in?",
      "value": "Austin, TX",
      "choices": [],
      "allow_free_text": true
    },
    {
      "question_id": "skills_languages",
      "prompt": "Which programming languages do you use?",
      "value": "Python, SQL",
      "choices": [],
      "allow_free_text": true
    }
  ],
  "last_agent_reply": "Noted \u2014 tell me more when you're ready.",
  "needs": {
    "resume": false,
    "confirm": true,
    "provider_setup": false
  },
  "progress": {
    "answered": 19,
    "remaining": 0
  },
  "resume": {
    "provided": true,
    "chars": 133,
    "pending": false
  },
  "review_fields": [
    {
      "id": "full_name",
      "prompt": "What's your full name?",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "education",
      "prompt": "What's your highest education?",
      "value": "Bachelor's",
      "choices": [
        {
          "value": "Bachelor's",
          "label": "Bachelor's"
        },
        {
          "value": "Master's",
          "label": "Master's"
        },
        {
          "value": "PhD",
          "label": "PhD"
        },
        {
          "value": "Self-taught",
          "label": "Self-taught"
        }
      ],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "experience_years",
      "prompt": "How many years have you worked in your field?",
      "value": "0\u20132",
      "choices": [
        {
          "value": "0\u20132",
          "label": "0\u20132"
        },
        {
          "value": "3\u20135",
          "label": "3\u20135"
        },
        {
          "value": "6\u201310",
          "label": "6\u201310"
        },
        {
          "value": "10+",
          "label": "10+"
        }
      ],
      "allow_free_text": false,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "work_style",
      "prompt": "How do you want to work?",
      "value": "remote",
      "choices": [
        {
          "value": "remote",
          "label": "Remote"
        },
        {
          "value": "hybrid",
          "label": "Hybrid"
        },
        {
          "value": "onsite",
          "label": "On-site"
        }
      ],
      "allow_free_text": false,
      "from_resume": true,
      "needs_answer": false
    },
    {
      "id": "country",
      "prompt": "Which country will you be working from / applying to jobs in?",
      "value": "United States",
      "choices": [
        {
          "value": "United States",
          "label": "United States"
        },
        {
          "value": "United Kingdom",
          "label": "United Kingdom"
        },
        {
          "value": "Canada",
          "label": "Canada"
        },
        {
          "value": "European Union",
          "label": "European Union"
        },
        {
          "value": "India",
          "label": "India"
        },
        {
          "value": "Other",
          "label": "Other"
        },
        {
          "value": "Not sure yet",
          "label": "Not sure yet"
        }
      ],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "work_authorized",
      "prompt": "Are you legally authorized to work in the country you're applying in?",
      "value": "yes",
      "choices": [
        {
          "value": "yes",
          "label": "Yes"
        },
        {
          "value": "no",
          "label": "No"
        }
      ],
      "allow_free_text": false,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "sponsorship",
      "prompt": "Will you now or in the future need visa sponsorship?",
      "value": "yes",
      "choices": [
        {
          "value": "yes",
          "label": "Yes"
        },
        {
          "value": "no",
          "label": "No"
        },
        {
          "value": "not_sure",
          "label": "Not sure"
        }
      ],
      "allow_free_text": false,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "permit_type",
      "prompt": "Which best describes your right to work?",
      "value": "Citizen",
      "choices": [
        {
          "value": "Citizen",
          "label": "Citizen"
        },
        {
          "value": "Permanent resident",
          "label": "Permanent resident"
        },
        {
          "value": "Open work permit",
          "label": "Open work permit"
        },
        {
          "value": "H-1B",
          "label": "H-1B"
        }
      ],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "currency",
      "prompt": "Which currency should salary conversations use?",
      "value": "USD",
      "choices": [
        {
          "value": "USD",
          "label": "USD"
        },
        {
          "value": "EUR",
          "label": "EUR"
        },
        {
          "value": "GBP",
          "label": "GBP"
        },
        {
          "value": "CAD",
          "label": "CAD"
        },
        {
          "value": "INR",
          "label": "INR"
        }
      ],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "salary_band",
      "prompt": "What's the minimum annual salary you'd accept?",
      "value": "60000",
      "choices": [
        {
          "value": "60000",
          "label": "$60k \u2013 $80k"
        },
        {
          "value": "80000",
          "label": "$80k \u2013 $100k"
        },
        {
          "value": "100000",
          "label": "$100k \u2013 $120k"
        },
        {
          "value": "120000",
          "label": "$120k+"
        }
      ],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "start_date",
      "prompt": "When could you start?",
      "value": "Immediately",
      "choices": [
        {
          "value": "Immediately",
          "label": "Immediately"
        },
        {
          "value": "In 2 weeks",
          "label": "In 2 weeks"
        },
        {
          "value": "In 1 month",
          "label": "In 1 month"
        },
        {
          "value": "In 3 months",
          "label": "In 3 months"
        }
      ],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "target_roles",
      "prompt": "Which job titles should we search for? One per line.",
      "value": "Data Engineer",
      "choices": [],
      "allow_free_text": true,
      "from_resume": true,
      "needs_answer": false
    },
    {
      "id": "location",
      "prompt": "What city are you based in?",
      "value": "Austin, TX",
      "choices": [],
      "allow_free_text": true,
      "from_resume": true,
      "needs_answer": false
    },
    {
      "id": "skills_languages",
      "prompt": "Which programming languages do you use?",
      "value": "Python, SQL",
      "choices": [],
      "allow_free_text": true,
      "from_resume": true,
      "needs_answer": false
    },
    {
      "id": "skills_frameworks",
      "prompt": "Which frameworks or libraries do you use?",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "skills_tools",
      "prompt": "Which tools do you work with?",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "email",
      "prompt": "What email address should applications use?",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "phone",
      "prompt": "What phone number should applications use?",
      "value": "555-0199",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "city",
      "prompt": "What city do you live in?",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "linkedin_url",
      "prompt": "Link your LinkedIn profile (optional)",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "github_url",
      "prompt": "Link your GitHub profile (optional)",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "website_url",
      "prompt": "Link a personal website (optional)",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "portfolio_url",
      "prompt": "Link a portfolio site (optional)",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    }
  ],
  "missing_review_fields": []
}
```

- PASS: review --set lands

```bash
$ applypilot agent keep-editing
```

```json
{
  "phase": "interview",
  "mode": "choices",
  "backend": "claude",
  "current_question": null,
  "prefill_chips": [
    {
      "question_id": "work_style",
      "prompt": "How do you want to work?",
      "value": "remote",
      "choices": [
        {
          "value": "remote",
          "label": "Remote"
        },
        {
          "value": "hybrid",
          "label": "Hybrid"
        },
        {
          "value": "onsite",
          "label": "On-site"
        }
      ],
      "allow_free_text": false
    },
    {
      "question_id": "target_roles",
      "prompt": "Which job titles should we search for? One per line.",
      "value": "Data Engineer",
      "choices": [],
      "allow_free_text": true
    },
    {
      "question_id": "location",
      "prompt": "What city are you based in?",
      "value": "Austin, TX",
      "choices": [],
      "allow_free_text": true
    },
    {
      "question_id": "skills_languages",
      "prompt": "Which programming languages do you use?",
      "value": "Python, SQL",
      "choices": [],
      "allow_free_text": true
    }
  ],
  "last_agent_reply": "Noted \u2014 tell me more when you're ready.",
  "needs": {
    "resume": false,
    "confirm": false,
    "provider_setup": false
  },
  "progress": {
    "answered": 19,
    "remaining": 0
  },
  "resume": {
    "provided": true,
    "chars": 133,
    "pending": false
  }
}
```

- PASS: keep-editing → back to interview

```bash
$ applypilot agent send --text "Lock it in with everything we discussed."
```

```json
{
  "phase": "review",
  "mode": "choices",
  "backend": "claude",
  "current_question": null,
  "prefill_chips": [
    {
      "question_id": "work_style",
      "prompt": "How do you want to work?",
      "value": "remote",
      "choices": [
        {
          "value": "remote",
          "label": "Remote"
        },
        {
          "value": "hybrid",
          "label": "Hybrid"
        },
        {
          "value": "onsite",
          "label": "On-site"
        }
      ],
      "allow_free_text": false
    },
    {
      "question_id": "target_roles",
      "prompt": "Which job titles should we search for? One per line.",
      "value": "Data Engineer, Analytics Engineer",
      "choices": [],
      "allow_free_text": true
    },
    {
      "question_id": "location",
      "prompt": "What city are you based in?",
      "value": "Austin, TX",
      "choices": [],
      "allow_free_text": true
    },
    {
      "question_id": "skills_languages",
      "prompt": "Which programming languages do you use?",
      "value": "Python, SQL",
      "choices": [],
      "allow_free_text": true
    }
  ],
  "last_agent_reply": "Perfect \u2014 here is everything we discussed.\n\nHere is your profile.\n\n[draft extracted \u2014 fenced text redacted; rerun with --reveal]\n",
  "needs": {
    "resume": false,
    "confirm": true,
    "provider_setup": false
  },
  "progress": {
    "answered": 19,
    "remaining": 0
  },
  "resume": {
    "provided": true,
    "chars": 133,
    "pending": false
  },
  "review_fields": [
    {
      "id": "full_name",
      "prompt": "What's your full name?",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "education",
      "prompt": "What's your highest education?",
      "value": "Bachelor's",
      "choices": [
        {
          "value": "Bachelor's",
          "label": "Bachelor's"
        },
        {
          "value": "Master's",
          "label": "Master's"
        },
        {
          "value": "PhD",
          "label": "PhD"
        },
        {
          "value": "Self-taught",
          "label": "Self-taught"
        }
      ],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "experience_years",
      "prompt": "How many years have you worked in your field?",
      "value": "0\u20132",
      "choices": [
        {
          "value": "0\u20132",
          "label": "0\u20132"
        },
        {
          "value": "3\u20135",
          "label": "3\u20135"
        },
        {
          "value": "6\u201310",
          "label": "6\u201310"
        },
        {
          "value": "10+",
          "label": "10+"
        }
      ],
      "allow_free_text": false,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "work_style",
      "prompt": "How do you want to work?",
      "value": "remote",
      "choices": [
        {
          "value": "remote",
          "label": "Remote"
        },
        {
          "value": "hybrid",
          "label": "Hybrid"
        },
        {
          "value": "onsite",
          "label": "On-site"
        }
      ],
      "allow_free_text": false,
      "from_resume": true,
      "needs_answer": false
    },
    {
      "id": "country",
      "prompt": "Which country will you be working from / applying to jobs in?",
      "value": "United States",
      "choices": [
        {
          "value": "United States",
          "label": "United States"
        },
        {
          "value": "United Kingdom",
          "label": "United Kingdom"
        },
        {
          "value": "Canada",
          "label": "Canada"
        },
        {
          "value": "European Union",
          "label": "European Union"
        },
        {
          "value": "India",
          "label": "India"
        },
        {
          "value": "Other",
          "label": "Other"
        },
        {
          "value": "Not sure yet",
          "label": "Not sure yet"
        }
      ],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "work_authorized",
      "prompt": "Are you legally authorized to work in the country you're applying in?",
      "value": "yes",
      "choices": [
        {
          "value": "yes",
          "label": "Yes"
        },
        {
          "value": "no",
          "label": "No"
        }
      ],
      "allow_free_text": false,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "sponsorship",
      "prompt": "Will you now or in the future need visa sponsorship?",
      "value": "yes",
      "choices": [
        {
          "value": "yes",
          "label": "Yes"
        },
        {
          "value": "no",
          "label": "No"
        },
        {
          "value": "not_sure",
          "label": "Not sure"
        }
      ],
      "allow_free_text": false,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "permit_type",
      "prompt": "Which best describes your right to work?",
      "value": "Citizen",
      "choices": [
        {
          "value": "Citizen",
          "label": "Citizen"
        },
        {
          "value": "Permanent resident",
          "label": "Permanent resident"
        },
        {
          "value": "Open work permit",
          "label": "Open work permit"
        },
        {
          "value": "H-1B",
          "label": "H-1B"
        }
      ],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "currency",
      "prompt": "Which currency should salary conversations use?",
      "value": "USD",
      "choices": [
        {
          "value": "USD",
          "label": "USD"
        },
        {
          "value": "EUR",
          "label": "EUR"
        },
        {
          "value": "GBP",
          "label": "GBP"
        },
        {
          "value": "CAD",
          "label": "CAD"
        },
        {
          "value": "INR",
          "label": "INR"
        }
      ],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "salary_band",
      "prompt": "What's the minimum annual salary you'd accept?",
      "value": "60000",
      "choices": [
        {
          "value": "60000",
          "label": "$60k \u2013 $80k"
        },
        {
          "value": "80000",
          "label": "$80k \u2013 $100k"
        },
        {
          "value": "100000",
          "label": "$100k \u2013 $120k"
        },
        {
          "value": "120000",
          "label": "$120k+"
        }
      ],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "start_date",
      "prompt": "When could you start?",
      "value": "Immediately",
      "choices": [
        {
          "value": "Immediately",
          "label": "Immediately"
        },
        {
          "value": "In 2 weeks",
          "label": "In 2 weeks"
        },
        {
          "value": "In 1 month",
          "label": "In 1 month"
        },
        {
          "value": "In 3 months",
          "label": "In 3 months"
        }
      ],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "target_roles",
      "prompt": "Which job titles should we search for? One per line.",
      "value": "Data Engineer, Analytics Engineer",
      "choices": [],
      "allow_free_text": true,
      "from_resume": true,
      "needs_answer": false
    },
    {
      "id": "location",
      "prompt": "What city are you based in?",
      "value": "Austin, TX",
      "choices": [],
      "allow_free_text": true,
      "from_resume": true,
      "needs_answer": false
    },
    {
      "id": "skills_languages",
      "prompt": "Which programming languages do you use?",
      "value": "Python, SQL",
      "choices": [],
      "allow_free_text": true,
      "from_resume": true,
      "needs_answer": false
    },
    {
      "id": "skills_frameworks",
      "prompt": "Which frameworks or libraries do you use?",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "skills_tools",
      "prompt": "Which tools do you work with?",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "email",
      "prompt": "What email address should applications use?",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "phone",
      "prompt": "What phone number should applications use?",
      "value": "555-0199",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "city",
      "prompt": "What city do you live in?",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "linkedin_url",
      "prompt": "Link your LinkedIn profile (optional)",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "github_url",
      "prompt": "Link your GitHub profile (optional)",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "website_url",
      "prompt": "Link a personal website (optional)",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "portfolio_url",
      "prompt": "Link a portfolio site (optional)",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    }
  ],
  "missing_review_fields": []
}
```

- PASS: draft hand-off → review

- PASS: send → fenced draft redacted by default — Perfect — here is everything we discussed.

Here is your pro

- PASS: redacted draft carries no raw profile JSON

```bash
$ applypilot agent keep-editing
```

```json
{
  "phase": "interview",
  "mode": "choices",
  "backend": "claude",
  "current_question": null,
  "prefill_chips": [
    {
      "question_id": "work_style",
      "prompt": "How do you want to work?",
      "value": "remote",
      "choices": [
        {
          "value": "remote",
          "label": "Remote"
        },
        {
          "value": "hybrid",
          "label": "Hybrid"
        },
        {
          "value": "onsite",
          "label": "On-site"
        }
      ],
      "allow_free_text": false
    },
    {
      "question_id": "target_roles",
      "prompt": "Which job titles should we search for? One per line.",
      "value": "Data Engineer, Analytics Engineer",
      "choices": [],
      "allow_free_text": true
    },
    {
      "question_id": "location",
      "prompt": "What city are you based in?",
      "value": "Austin, TX",
      "choices": [],
      "allow_free_text": true
    },
    {
      "question_id": "skills_languages",
      "prompt": "Which programming languages do you use?",
      "value": "Python, SQL",
      "choices": [],
      "allow_free_text": true
    }
  ],
  "last_agent_reply": "Perfect \u2014 here is everything we discussed.\n\nHere is your profile.\n\n[draft extracted \u2014 fenced text redacted; rerun with --reveal]\n",
  "needs": {
    "resume": false,
    "confirm": false,
    "provider_setup": false
  },
  "progress": {
    "answered": 19,
    "remaining": 0
  },
  "resume": {
    "provided": true,
    "chars": 133,
    "pending": false
  }
}
```

- PASS: second keep-editing → interview

```bash
$ applypilot agent agent-led on
```

```json
{
  "phase": "interview",
  "mode": "agent",
  "backend": "claude",
  "current_question": null,
  "prefill_chips": [
    {
      "question_id": "work_style",
      "prompt": "How do you want to work?",
      "value": "remote",
      "choices": [
        {
          "value": "remote",
          "label": "Remote"
        },
        {
          "value": "hybrid",
          "label": "Hybrid"
        },
        {
          "value": "onsite",
          "label": "On-site"
        }
      ],
      "allow_free_text": false
    },
    {
      "question_id": "target_roles",
      "prompt": "Which job titles should we search for? One per line.",
      "value": "Data Engineer, Analytics Engineer",
      "choices": [],
      "allow_free_text": true
    },
    {
      "question_id": "location",
      "prompt": "What city are you based in?",
      "value": "Austin, TX",
      "choices": [],
      "allow_free_text": true
    },
    {
      "question_id": "skills_languages",
      "prompt": "Which programming languages do you use?",
      "value": "Python, SQL",
      "choices": [],
      "allow_free_text": true
    }
  ],
  "last_agent_reply": "Got it \u2014 I'll take it from here. What's your full name?",
  "needs": {
    "resume": false,
    "confirm": false,
    "provider_setup": false
  },
  "progress": {
    "answered": 19,
    "remaining": 0
  },
  "resume": {
    "provided": true,
    "chars": 133,
    "pending": false
  }
}
```

```bash
$ applypilot agent agent-led off
```

```json
{
  "phase": "review",
  "mode": "choices",
  "backend": "claude",
  "current_question": null,
  "prefill_chips": [
    {
      "question_id": "work_style",
      "prompt": "How do you want to work?",
      "value": "remote",
      "choices": [
        {
          "value": "remote",
          "label": "Remote"
        },
        {
          "value": "hybrid",
          "label": "Hybrid"
        },
        {
          "value": "onsite",
          "label": "On-site"
        }
      ],
      "allow_free_text": false
    },
    {
      "question_id": "target_roles",
      "prompt": "Which job titles should we search for? One per line.",
      "value": "Data Engineer, Analytics Engineer",
      "choices": [],
      "allow_free_text": true
    },
    {
      "question_id": "location",
      "prompt": "What city are you based in?",
      "value": "Austin, TX",
      "choices": [],
      "allow_free_text": true
    },
    {
      "question_id": "skills_languages",
      "prompt": "Which programming languages do you use?",
      "value": "Python, SQL",
      "choices": [],
      "allow_free_text": true
    }
  ],
  "last_agent_reply": "Got it \u2014 I'll take it from here. What's your full name?",
  "needs": {
    "resume": false,
    "confirm": true,
    "provider_setup": false
  },
  "progress": {
    "answered": 19,
    "remaining": 0
  },
  "resume": {
    "provided": true,
    "chars": 133,
    "pending": false
  },
  "review_fields": [
    {
      "id": "full_name",
      "prompt": "What's your full name?",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "education",
      "prompt": "What's your highest education?",
      "value": "Bachelor's",
      "choices": [
        {
          "value": "Bachelor's",
          "label": "Bachelor's"
        },
        {
          "value": "Master's",
          "label": "Master's"
        },
        {
          "value": "PhD",
          "label": "PhD"
        },
        {
          "value": "Self-taught",
          "label": "Self-taught"
        }
      ],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "experience_years",
      "prompt": "How many years have you worked in your field?",
      "value": "0\u20132",
      "choices": [
        {
          "value": "0\u20132",
          "label": "0\u20132"
        },
        {
          "value": "3\u20135",
          "label": "3\u20135"
        },
        {
          "value": "6\u201310",
          "label": "6\u201310"
        },
        {
          "value": "10+",
          "label": "10+"
        }
      ],
      "allow_free_text": false,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "work_style",
      "prompt": "How do you want to work?",
      "value": "remote",
      "choices": [
        {
          "value": "remote",
          "label": "Remote"
        },
        {
          "value": "hybrid",
          "label": "Hybrid"
        },
        {
          "value": "onsite",
          "label": "On-site"
        }
      ],
      "allow_free_text": false,
      "from_resume": true,
      "needs_answer": false
    },
    {
      "id": "country",
      "prompt": "Which country will you be working from / applying to jobs in?",
      "value": "United States",
      "choices": [
        {
          "value": "United States",
          "label": "United States"
        },
        {
          "value": "United Kingdom",
          "label": "United Kingdom"
        },
        {
          "value": "Canada",
          "label": "Canada"
        },
        {
          "value": "European Union",
          "label": "European Union"
        },
        {
          "value": "India",
          "label": "India"
        },
        {
          "value": "Other",
          "label": "Other"
        },
        {
          "value": "Not sure yet",
          "label": "Not sure yet"
        }
      ],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "work_authorized",
      "prompt": "Are you legally authorized to work in the country you're applying in?",
      "value": "yes",
      "choices": [
        {
          "value": "yes",
          "label": "Yes"
        },
        {
          "value": "no",
          "label": "No"
        }
      ],
      "allow_free_text": false,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "sponsorship",
      "prompt": "Will you now or in the future need visa sponsorship?",
      "value": "yes",
      "choices": [
        {
          "value": "yes",
          "label": "Yes"
        },
        {
          "value": "no",
          "label": "No"
        },
        {
          "value": "not_sure",
          "label": "Not sure"
        }
      ],
      "allow_free_text": false,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "permit_type",
      "prompt": "Which best describes your right to work?",
      "value": "Citizen",
      "choices": [
        {
          "value": "Citizen",
          "label": "Citizen"
        },
        {
          "value": "Permanent resident",
          "label": "Permanent resident"
        },
        {
          "value": "Open work permit",
          "label": "Open work permit"
        },
        {
          "value": "H-1B",
          "label": "H-1B"
        }
      ],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "currency",
      "prompt": "Which currency should salary conversations use?",
      "value": "USD",
      "choices": [
        {
          "value": "USD",
          "label": "USD"
        },
        {
          "value": "EUR",
          "label": "EUR"
        },
        {
          "value": "GBP",
          "label": "GBP"
        },
        {
          "value": "CAD",
          "label": "CAD"
        },
        {
          "value": "INR",
          "label": "INR"
        }
      ],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "salary_band",
      "prompt": "What's the minimum annual salary you'd accept?",
      "value": "60000",
      "choices": [
        {
          "value": "60000",
          "label": "$60k \u2013 $80k"
        },
        {
          "value": "80000",
          "label": "$80k \u2013 $100k"
        },
        {
          "value": "100000",
          "label": "$100k \u2013 $120k"
        },
        {
          "value": "120000",
          "label": "$120k+"
        }
      ],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "start_date",
      "prompt": "When could you start?",
      "value": "Immediately",
      "choices": [
        {
          "value": "Immediately",
          "label": "Immediately"
        },
        {
          "value": "In 2 weeks",
          "label": "In 2 weeks"
        },
        {
          "value": "In 1 month",
          "label": "In 1 month"
        },
        {
          "value": "In 3 months",
          "label": "In 3 months"
        }
      ],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "target_roles",
      "prompt": "Which job titles should we search for? One per line.",
      "value": "Data Engineer, Analytics Engineer",
      "choices": [],
      "allow_free_text": true,
      "from_resume": true,
      "needs_answer": false
    },
    {
      "id": "location",
      "prompt": "What city are you based in?",
      "value": "Austin, TX",
      "choices": [],
      "allow_free_text": true,
      "from_resume": true,
      "needs_answer": false
    },
    {
      "id": "skills_languages",
      "prompt": "Which programming languages do you use?",
      "value": "Python, SQL",
      "choices": [],
      "allow_free_text": true,
      "from_resume": true,
      "needs_answer": false
    },
    {
      "id": "skills_frameworks",
      "prompt": "Which frameworks or libraries do you use?",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "skills_tools",
      "prompt": "Which tools do you work with?",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "email",
      "prompt": "What email address should applications use?",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "phone",
      "prompt": "What phone number should applications use?",
      "value": "555-0199",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "city",
      "prompt": "What city do you live in?",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "linkedin_url",
      "prompt": "Link your LinkedIn profile (optional)",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "github_url",
      "prompt": "Link your GitHub profile (optional)",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "website_url",
      "prompt": "Link a personal website (optional)",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "portfolio_url",
      "prompt": "Link a portfolio site (optional)",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    }
  ],
  "missing_review_fields": []
}
```

- PASS: agent-led round-trip → review

```bash
$ applypilot agent confirm
```

```json
{
  "phase": "done",
  "mode": "choices",
  "backend": "claude",
  "current_question": null,
  "prefill_chips": [
    {
      "question_id": "work_style",
      "prompt": "How do you want to work?",
      "value": "remote",
      "choices": [
        {
          "value": "remote",
          "label": "Remote"
        },
        {
          "value": "hybrid",
          "label": "Hybrid"
        },
        {
          "value": "onsite",
          "label": "On-site"
        }
      ],
      "allow_free_text": false
    },
    {
      "question_id": "target_roles",
      "prompt": "Which job titles should we search for? One per line.",
      "value": "Data Engineer, Analytics Engineer",
      "choices": [],
      "allow_free_text": true
    },
    {
      "question_id": "location",
      "prompt": "What city are you based in?",
      "value": "Austin, TX",
      "choices": [],
      "allow_free_text": true
    },
    {
      "question_id": "skills_languages",
      "prompt": "Which programming languages do you use?",
      "value": "Python, SQL",
      "choices": [],
      "allow_free_text": true
    }
  ],
  "last_agent_reply": "Got it \u2014 I'll take it from here. What's your full name?",
  "needs": {
    "resume": false,
    "confirm": false,
    "provider_setup": false
  },
  "progress": {
    "answered": 19,
    "remaining": 0
  },
  "resume": {
    "provided": true,
    "chars": 133,
    "pending": false
  },
  "written": {
    "profile": "/home/user/work/dogfood-pipeline/applypilot/profile.json",
    "profile_md": "/home/user/work/dogfood-pipeline/applypilot/profile.md",
    "searches_yaml": "/home/user/work/dogfood-pipeline/applypilot/searches.yaml"
  }
}
```

- PASS: confirm → phase done

- PASS: profile.json written

- PASS: searches.yaml written

- PASS: resume.txt on disk


## 4 · Pipeline: run discover enrich score, then run tailor cover pdf

```bash
$ applypilot run discover enrich score --min-score 7
```

```json
╭──────────────────────────────────╮
│ ApplyPilot Pipeline (sequential) │
╰──────────────────────────────────╯
  Min score:  7
  Workers:    1
  Validation: normal
  Stages:     discover -> enrich -> score
  DB:        0 jobs, 0 pending enrichment

======================================================================
  STAGE: discover — Job discovery (JobSpy + Workday + smart extract + Composio)
  Started: 00:26:56
======================================================================
  JobSpy full crawl...
  Workday corporate scraper...
  Smart extract (AI-powered scraping)...
  Composio (web3.career)...

  Stage 'discover' completed in 0.0s — ok

======================================================================
  STAGE: enrich — Detail enrichment (full descriptions + apply URLs)
  Started: 00:26:56
======================================================================

  Stage 'enrich' completed in 0.0s — ok

======================================================================
  STAGE: score — LLM scoring (fit 1-10)
  Started: 00:26:56
======================================================================

  Stage 'score' completed in 0.1s — ok

======================================================================
      Pipeline Summary      
┏━━━━━━━━━━┳━━━━━━━━┳━━━━━━┓
┃ Stage    ┃ Status ┃ Time ┃
┡━━━━━━━━━━╇━━━━━━━━╇━━━━━━┩
│ discover │ ok     │ 0.0s │
│ enrich   │ ok     │ 0.0s │
│ score    │ ok     │ 0.1s │
│          │        │      │
│ Total    │        │ 0.1s │
└──────────┴────────┴──────┘

  DB Final State:
    Total jobs:     2
    With desc:      2
    Scored:         2
    Tailored:       0
    Cover letters:  0
    Ready to apply: 0
    Applied:        0
======================================================================
```

- PASS: both jobs scored

- PASS: score==9 for https://jobs.example/alpha — got 9

- PASS: score==9 for https://jobs.example/beta — got 9

- PASS: stub provider saw 2 scoring calls — provider calls so far: [{"kind": "score", "marker": "SCORE_PROMPT"}, {"kind": "score", "marker": "SCORE_PROMPT"}]

```bash
$ applypilot run tailor cover pdf --min-score 7
```

```json
╭──────────────────────────────────╮
│ ApplyPilot Pipeline (sequential) │
╰──────────────────────────────────╯
  Min score:  7
  Workers:    1
  Validation: normal
  Stages:     tailor -> cover -> pdf
  DB:        2 jobs, 0 pending enrichment

======================================================================
  STAGE: tailor — Resume tailoring (LLM + validation)
  Started: 00:26:56
======================================================================

  Stage 'tailor' completed in 5.6s — ok

======================================================================
  STAGE: cover — Cover letter generation
  Started: 00:27:02
======================================================================

  Stage 'cover' completed in 4.6s — ok

======================================================================
  STAGE: pdf — PDF conversion (tailored resumes + cover letters)
  Started: 00:27:07
======================================================================

  Stage 'pdf' completed in 0.0s — ok

======================================================================
     Pipeline Summary      
┏━━━━━━━━┳━━━━━━━━┳━━━━━━━┓
┃ Stage  ┃ Status ┃  Time ┃
┡━━━━━━━━╇━━━━━━━━╇━━━━━━━┩
│ tailor │ ok     │  5.6s │
│ cover  │ ok     │  4.6s │
│ pdf    │ ok     │  0.0s │
│        │        │       │
│ Total  │        │ 10.2s │
└────────┴────────┴───────┘

  DB Final State:
    Total jobs:     2
    With desc:      2
    Scored:         2
    Tailored:       2
    Cover letters:  2
    Ready to apply: 2
    Applied:        0
======================================================================
```

- PASS: tailored resumes written — exampleboard_Analytics_Engineer_6f8529f6_v1.pdf, exampleboard_Analytics_Engineer_6f8529f6_v1.txt, exampleboard_Analytics_Engineer_6f8529f6_v1_JOB.txt, exampleboard_Analytics_Engineer_6f8529f6_v1_REPORT.json, exampleboard_Data_Engineer_942471be_v1.pdf, exampleboard_Data_Engineer_942471be_v1.txt, exampleboard_Data_Engineer_942471be_v1_JOB.txt, exampleboard_Data_Engineer_942471be_v1_REPORT.json

- PASS: cover letters written — exampleboard_Analytics_Engineer_6f8529f6_v1_CL.pdf, exampleboard_Analytics_Engineer_6f8529f6_v1_CL.txt, exampleboard_Data_Engineer_942471be_v1_CL.pdf, exampleboard_Data_Engineer_942471be_v1_CL.txt

- PASS: PDFs rendered — exampleboard_Analytics_Engineer_6f8529f6_v1.pdf, exampleboard_Data_Engineer_942471be_v1.pdf, exampleboard_Analytics_Engineer_6f8529f6_v1_CL.pdf, exampleboard_Data_Engineer_942471be_v1_CL.pdf

- PASS: exampleboard_Analytics_Engineer_6f8529f6_v1.pdf is a real PDF — b'%PDF-'

- PASS: exampleboard_Data_Engineer_942471be_v1.pdf is a real PDF — b'%PDF-'

- PASS: exampleboard_Analytics_Engineer_6f8529f6_v1_CL.pdf is a real PDF — b'%PDF-'

- PASS: exampleboard_Data_Engineer_942471be_v1_CL.pdf is a real PDF — b'%PDF-'

- PASS: queue row updated for https://jobs.example/alpha

- PASS: queue row updated for https://jobs.example/beta


## 5 · Apply: plan → confirm → the pinned batch runs (RESULT lines)

```bash
$ applypilot apply --plan --limit 2
```

```json
{
  "plan_hash": "e2e253a4d1bb138f04d7bfd8a11b62033ffdb935b14275d4f395e72c74fb6623",
  "day": "2026-11-08",
  "commits_nothing": true,
  "batch_size": 2,
  "min_score": 7,
  "cap": 5,
  "cap_source": "default",
  "applied_today": 0,
  "headroom": 5,
  "estimate_basis": "apply_timeout=300s per job (the run's watchdog budget \u2014 an upper bound)",
  "jobs": [
    {
      "url": "https://jobs.example/alpha",
      "application_url": "https://jobs.example/alpha",
      "title": "Data Engineer",
      "site": "exampleboard",
      "fit_score": 9,
      "estimate_seconds": 300
    },
    {
      "url": "https://jobs.example/beta",
      "application_url": "https://jobs.example/beta",
      "title": "Analytics Engineer",
      "site": "exampleboard",
      "fit_score": 9,
      "estimate_seconds": 300
    }
  ],
  "next": "applypilot apply --i-confirm e2e253a4d1bb138f04d7bfd8a11b62033ffdb935b14275d4f395e72c74fb6623 (same flags as this --plan run, same UTC day)"
}
```

- PASS: plan commits nothing

- PASS: plan batch == 2

- PASS: plan cap == 5/day default

- PASS: plan hash staged

- PASS: plan jobs == the tailored queue

```bash
$ applypilot apply --i-confirm e2e253a4d1bb138f04d7bfd8a11b62033ffdb935b14275d4f395e72c74fb6623 --headless
```

```json
Launching Auto-Apply
  Limit:    2
  Workers:  1
  Engine:   claude
  Model:    haiku
  Headless: True
  Dry run:  False

Launching apply pipeline (2 jobs, 1 worker, poll every 60s)...
Ctrl+C = skip current job(s) | Ctrl+C x2 = stop
                                         ApplyPilot Dashboard                                       
┏━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━┳━━━━┳━━━━━━━━━━━━━━━━━━━━━━┳━━━┳━━━━┳━━━━━━
┃  ┃ Job                             ┃  Status   ┃ Time ┃ A… ┃ Last Action          ┃ … ┃ F… ┃  Cost
┡━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━╇━━━━╇━━━━━━━━━━━━━━━━━━━━━━╇━━━╇━━━━╇━━━━━━
│  │ Analytics Engineer @ examplebo… │   DONE    │      │    │ finished             │ 1 │  1 │ $0.0…
├──┼─────────────────────────────────┼───────────┼──────┼────┼──────────────────────┼───┼────┼──────
│  │                                 │           │      │    │ TOTAL                │ 1 │  1 │ $0.0…
└──┴─────────────────────────────────┴───────────┴──────┴────┴──────────────────────┴───┴────┴──────
╭───────────────────────────────────────── Recent Events ──────────────────────────────────────────╮
│ 00:27:07 [W0] Launching Chrome...                                                                │
│ 00:27:10 [W0] Starting: Data Engineer @ exampleboard                                             │
│ 00:27:10 [W0] APPLIED (0s): Data Engineer                                                        │
│ 00:27:10 [W0] Launching Chrome...                                                                │
│ 00:27:13 [W0] Starting: Analytics Engineer @ exampleboard                                        │
│ 00:27:14 [W0] CAPTCHA (0s): Analytics Engineer                                                   │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
Done: 1 applied, 1 failed ($0.002)
Logs: /home/user/work/dogfood-pipeline/applypilot/logs
```

- PASS: run events narrate APPLIED

- PASS: run events narrate CAPTCHA

- PASS: staged plan consumed

- engine run logs (2): claude_20261108_002710_w0_exampleboard.txt, claude_20261108_002714_w0_exampleboard.txt

```
Opened the posting, completed the application form, and submitted. RESULT:APPLIED
Opened the posting, completed the application form, and submitted. RESULT:APPLIED
Opened the posting, but a captcha blocked submission. RESULT:CAPTCHA — retry later.
Opened the posting, but a captcha blocked submission. RESULT:CAPTCHA — retry later.
```

- PASS: engine narrates RESULT:APPLIED

- PASS: engine narrates RESULT:CAPTCHA

- PASS: alpha recorded applied — {"url": "https://jobs.example/alpha", "apply_status": "applied", "apply_error": null, "apply_attempts": 0, "applied_at": "2026-11-08T00:27:10.623995+00:00"}

- PASS: beta terminal, not applied (CAPTCHA) — {"url": "https://jobs.example/beta", "apply_status": "captcha", "apply_error": "captcha", "apply_attempts": 99, "applied_at": null}

- PASS: beta persists distinct captcha status, attempts pinned — {"url": "https://jobs.example/beta", "apply_status": "captcha", "apply_error": "captcha", "apply_attempts": 99, "applied_at": null}

```bash
$ applypilot status --json
```

```json
{
  "total": 2,
  "by_site": [
    [
      "exampleboard",
      2
    ]
  ],
  "pending_detail": 0,
  "with_description": 2,
  "detail_errors": 0,
  "scored": 2,
  "unscored": 0,
  "score_distribution": [
    [
      9,
      2
    ]
  ],
  "score_provenance": {
    "host": 0,
    "provider": 2,
    "unattributed": 0
  },
  "tailored": 2,
  "untailored_eligible": 0,
  "tailor_exhausted": 0,
  "with_cover_letter": 2,
  "cover_exhausted": 0,
  "applied": 1,
  "captcha": 1,
  "apply_errors": 0,
  "ready_to_apply": 1
}
```

- PASS: status --json counts 1 applied — applied=1, total=2

- PASS: status --json counts captcha separately from failures — captcha=1, apply_errors=0

```bash
$ applypilot apply --i-confirm e2e253a4d1bb138f04d7bfd8a11b62033ffdb935b14275d4f395e72c74fb6623
```

```
(exit 1)
{
  "error": {
    "code": "plan_required",
    "message": "no plan is staged \u2014 run 'applypilot apply --plan' first, then confirm with the plan hash it printed."
  }
}
```

- PASS: refusal envelope code == plan_required — got 'plan_required'


## 6 · Closing probes: redo wipes the interview, profile survives

```bash
$ applypilot agent redo
```

```json
{
  "phase": "interview",
  "mode": "choices",
  "backend": "claude",
  "current_question": {
    "id": "full_name",
    "prompt": "What's your full name?",
    "choices": [],
    "prefilled": null,
    "allow_free_text": true,
    "free_text_kind": null
  },
  "prefill_chips": [],
  "last_agent_reply": "Hello! Tell me about your background.",
  "needs": {
    "resume": false,
    "confirm": false,
    "provider_setup": false
  },
  "progress": {
    "answered": 0,
    "remaining": 18
  },
  "resume": {
    "provided": true,
    "chars": 133,
    "pending": false
  }
}
```

- PASS: redo → fresh interview (observed semantics) — phase=interview, answered=0

- PASS: redo resets the agenda

- PASS: profile.json survived redo

```bash
$ applypilot agent next
```

```json
{
  "phase": "interview",
  "mode": "choices",
  "backend": "claude",
  "current_question": {
    "id": "full_name",
    "prompt": "What's your full name?",
    "choices": [],
    "prefilled": null,
    "allow_free_text": true,
    "free_text_kind": null
  },
  "prefill_chips": [],
  "last_agent_reply": "Hello! Tell me about your background.",
  "needs": {
    "resume": false,
    "confirm": false,
    "provider_setup": false
  },
  "progress": {
    "answered": 0,
    "remaining": 18
  },
  "resume": {
    "provided": true,
    "chars": 133,
    "pending": false
  }
}
```

- PASS: next after redo → fresh guided interview


## 7 · Host-driven pipeline journey: score → enrich → tailor → cover → apply, zero provider calls

```bash
$ applypilot agent start
```

```json
{
  "phase": "intake",
  "mode": "choices",
  "backend": "",
  "current_question": null,
  "prefill_chips": [],
  "last_agent_reply": null,
  "needs": {
    "resume": true,
    "confirm": false,
    "provider_setup": true
  },
  "progress": {
    "answered": 0,
    "remaining": 18
  },
  "resume": {
    "provided": true,
    "chars": 133,
    "pending": false
  }
}
```

- PASS: journey start → phase intake (no backend chosen yet) — phase=intake, backend=''

```bash
$ applypilot agent resume --file /home/user/work/dogfood-pipeline/resume.txt
```

```json
{
  "phase": "intake",
  "mode": "choices",
  "backend": "",
  "current_question": null,
  "prefill_chips": [],
  "last_agent_reply": null,
  "needs": {
    "resume": false,
    "confirm": false,
    "provider_setup": true
  },
  "progress": {
    "answered": 0,
    "remaining": 18
  },
  "resume": {
    "provided": true,
    "chars": 133,
    "pending": true
  }
}
```

```bash
$ applypilot agent start
```

```json
{
  "phase": "interview",
  "mode": "choices",
  "backend": "host",
  "current_question": {
    "id": "full_name",
    "prompt": "What's your full name?",
    "choices": [],
    "prefilled": null,
    "allow_free_text": true,
    "free_text_kind": null
  },
  "prefill_chips": [],
  "last_agent_reply": null,
  "needs": {
    "resume": false,
    "confirm": false,
    "provider_setup": true
  },
  "progress": {
    "answered": 0,
    "remaining": 18
  },
  "resume": {
    "provided": true,
    "chars": 133,
    "pending": true
  }
}
```

- PASS: journey interview opens on host (no kickoff turn)

```bash
$ applypilot agent next
```

```json
{"phase":"interview","mode":"choices","backend":"host","current_question":{"id":"full_name","prompt":"What's your full name?","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[],"last_agent_reply":null,"needs":{"resume":false,"confirm":false,"provider_setup":true},"progress":{"answered":0,"remaining":18},"resume":{"provided":true,"chars":133,"pending":true}}
```

```bash
$ applypilot agent answer --question full_name --free-text "Something reasonable"
```

```json
{"phase":"interview","mode":"choices","backend":"host","current_question":{"id":"education","prompt":"What's your highest education?","choices":[{"value":"Bachelor's","label":"Bachelor's"},{"value":"Master's","label":"Master's"},{"value":"PhD","label":"PhD"},{"value":"Self-taught","label":"Self-taught"}],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[],"last_agent_reply":null,"needs":{"resume":false,"confirm":false,"provider_setup":true},"progress":{"answered":1,"remaining":17},"resume":{"provided":true,"chars":133,"pending":true}}
```

```bash
$ applypilot agent next
```

```json
{"phase":"interview","mode":"choices","backend":"host","current_question":{"id":"education","prompt":"What's your highest education?","choices":[{"value":"Bachelor's","label":"Bachelor's"},{"value":"Master's","label":"Master's"},{"value":"PhD","label":"PhD"},{"value":"Self-taught","label":"Self-taught"}],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[],"last_agent_reply":null,"needs":{"resume":false,"confirm":false,"provider_setup":true},"progress":{"answered":1,"remaining":17},"resume":{"provided":true,"chars":133,"pending":true}}
```

```bash
$ applypilot agent answer --question education --choice Bachelor's
```

```json
{"phase":"interview","mode":"choices","backend":"host","current_question":{"id":"experience_years","prompt":"How many years have you worked in your field?","choices":[{"value":"0\u20132","label":"0\u20132"},{"value":"3\u20135","label":"3\u20135"},{"value":"6\u201310","label":"6\u201310"},{"value":"10+","label":"10+"}],"prefilled":null,"allow_free_text":false,"free_text_kind":null},"prefill_chips":[],"last_agent_reply":null,"needs":{"resume":false,"confirm":false,"provider_setup":true},"progress":{"answered":2,"remaining":16},"resume":{"provided":true,"chars":133,"pending":true}}
```

```bash
$ applypilot agent next
```

```json
{"phase":"interview","mode":"choices","backend":"host","current_question":{"id":"experience_years","prompt":"How many years have you worked in your field?","choices":[{"value":"0\u20132","label":"0\u20132"},{"value":"3\u20135","label":"3\u20135"},{"value":"6\u201310","label":"6\u201310"},{"value":"10+","label":"10+"}],"prefilled":null,"allow_free_text":false,"free_text_kind":null},"prefill_chips":[],"last_agent_reply":null,"needs":{"resume":false,"confirm":false,"provider_setup":true},"progress":{"answered":2,"remaining":16},"resume":{"provided":true,"chars":133,"pending":true}}
```

```bash
$ applypilot agent answer --question experience_years --choice 0–2
```

```json
{"phase":"interview","mode":"choices","backend":"host","current_question":{"id":"work_style","prompt":"How do you want to work?","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"prefilled":null,"allow_free_text":false,"free_text_kind":null},"prefill_chips":[],"last_agent_reply":null,"needs":{"resume":false,"confirm":false,"provider_setup":true},"progress":{"answered":3,"remaining":15},"resume":{"provided":true,"chars":133,"pending":true}}
```

```bash
$ applypilot agent next
```

```json
{"phase":"interview","mode":"choices","backend":"host","current_question":{"id":"work_style","prompt":"How do you want to work?","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"prefilled":null,"allow_free_text":false,"free_text_kind":null},"prefill_chips":[],"last_agent_reply":null,"needs":{"resume":false,"confirm":false,"provider_setup":true},"progress":{"answered":3,"remaining":15},"resume":{"provided":true,"chars":133,"pending":true}}
```

```bash
$ applypilot agent answer --question work_style --choice remote
```

```json
{"phase":"interview","mode":"choices","backend":"host","current_question":{"id":"country","prompt":"Which country will you be working from / applying to jobs in?","choices":[{"value":"United States","label":"United States"},{"value":"United Kingdom","label":"United Kingdom"},{"value":"Canada","label":"Canada"},{"value":"European Union","label":"European Union"},{"value":"India","label":"India"},{"value":"Other","label":"Other"},{"value":"Not sure yet","label":"Not sure yet"}],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[],"last_agent_reply":null,"needs":{"resume":false,"confirm":false,"provider_setup":true},"progress":{"answered":4,"remaining":14},"resume":{"provided":true,"chars":133,"pending":true}}
```

```bash
$ applypilot agent next
```

```json
{"phase":"interview","mode":"choices","backend":"host","current_question":{"id":"country","prompt":"Which country will you be working from / applying to jobs in?","choices":[{"value":"United States","label":"United States"},{"value":"United Kingdom","label":"United Kingdom"},{"value":"Canada","label":"Canada"},{"value":"European Union","label":"European Union"},{"value":"India","label":"India"},{"value":"Other","label":"Other"},{"value":"Not sure yet","label":"Not sure yet"}],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[],"last_agent_reply":null,"needs":{"resume":false,"confirm":false,"provider_setup":true},"progress":{"answered":4,"remaining":14},"resume":{"provided":true,"chars":133,"pending":true}}
```

```bash
$ applypilot agent answer --question country --choice "United States"
```

```json
{"phase":"interview","mode":"choices","backend":"host","current_question":{"id":"work_authorized","prompt":"Are you legally authorized to work in the country you're applying in?","choices":[{"value":"yes","label":"Yes"},{"value":"no","label":"No"}],"prefilled":null,"allow_free_text":false,"free_text_kind":null},"prefill_chips":[],"last_agent_reply":null,"needs":{"resume":false,"confirm":false,"provider_setup":true},"progress":{"answered":5,"remaining":16},"resume":{"provided":true,"chars":133,"pending":true}}
```

```bash
$ applypilot agent next
```

```json
{"phase":"interview","mode":"choices","backend":"host","current_question":{"id":"work_authorized","prompt":"Are you legally authorized to work in the country you're applying in?","choices":[{"value":"yes","label":"Yes"},{"value":"no","label":"No"}],"prefilled":null,"allow_free_text":false,"free_text_kind":null},"prefill_chips":[],"last_agent_reply":null,"needs":{"resume":false,"confirm":false,"provider_setup":true},"progress":{"answered":5,"remaining":16},"resume":{"provided":true,"chars":133,"pending":true}}
```

```bash
$ applypilot agent answer --question work_authorized --choice yes
```

```json
{"phase":"interview","mode":"choices","backend":"host","current_question":{"id":"sponsorship","prompt":"Will you now or in the future need visa sponsorship?","choices":[{"value":"yes","label":"Yes"},{"value":"no","label":"No"},{"value":"not_sure","label":"Not sure"}],"prefilled":null,"allow_free_text":false,"free_text_kind":null},"prefill_chips":[],"last_agent_reply":null,"needs":{"resume":false,"confirm":false,"provider_setup":true},"progress":{"answered":6,"remaining":16},"resume":{"provided":true,"chars":133,"pending":true}}
```

```bash
$ applypilot agent next
```

```json
{"phase":"interview","mode":"choices","backend":"host","current_question":{"id":"sponsorship","prompt":"Will you now or in the future need visa sponsorship?","choices":[{"value":"yes","label":"Yes"},{"value":"no","label":"No"},{"value":"not_sure","label":"Not sure"}],"prefilled":null,"allow_free_text":false,"free_text_kind":null},"prefill_chips":[],"last_agent_reply":null,"needs":{"resume":false,"confirm":false,"provider_setup":true},"progress":{"answered":6,"remaining":16},"resume":{"provided":true,"chars":133,"pending":true}}
```

```bash
$ applypilot agent answer --question sponsorship --choice yes
```

```json
{"phase":"interview","mode":"choices","backend":"host","current_question":{"id":"permit_type","prompt":"Which best describes your right to work?","choices":[{"value":"Citizen","label":"Citizen"},{"value":"Permanent resident","label":"Permanent resident"},{"value":"Open work permit","label":"Open work permit"},{"value":"H-1B","label":"H-1B"}],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[],"last_agent_reply":null,"needs":{"resume":false,"confirm":false,"provider_setup":true},"progress":{"answered":7,"remaining":15},"resume":{"provided":true,"chars":133,"pending":true}}
```

```bash
$ applypilot agent next
```

```json
{"phase":"interview","mode":"choices","backend":"host","current_question":{"id":"permit_type","prompt":"Which best describes your right to work?","choices":[{"value":"Citizen","label":"Citizen"},{"value":"Permanent resident","label":"Permanent resident"},{"value":"Open work permit","label":"Open work permit"},{"value":"H-1B","label":"H-1B"}],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[],"last_agent_reply":null,"needs":{"resume":false,"confirm":false,"provider_setup":true},"progress":{"answered":7,"remaining":15},"resume":{"provided":true,"chars":133,"pending":true}}
```

```bash
$ applypilot agent answer --question permit_type --choice Citizen
```

```json
{"phase":"interview","mode":"choices","backend":"host","current_question":{"id":"currency","prompt":"Which currency should salary conversations use?","choices":[{"value":"USD","label":"USD"},{"value":"EUR","label":"EUR"},{"value":"GBP","label":"GBP"},{"value":"CAD","label":"CAD"},{"value":"INR","label":"INR"}],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[],"last_agent_reply":null,"needs":{"resume":false,"confirm":false,"provider_setup":true},"progress":{"answered":8,"remaining":14},"resume":{"provided":true,"chars":133,"pending":true}}
```

```bash
$ applypilot agent next
```

```json
{"phase":"interview","mode":"choices","backend":"host","current_question":{"id":"currency","prompt":"Which currency should salary conversations use?","choices":[{"value":"USD","label":"USD"},{"value":"EUR","label":"EUR"},{"value":"GBP","label":"GBP"},{"value":"CAD","label":"CAD"},{"value":"INR","label":"INR"}],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[],"last_agent_reply":null,"needs":{"resume":false,"confirm":false,"provider_setup":true},"progress":{"answered":8,"remaining":14},"resume":{"provided":true,"chars":133,"pending":true}}
```

```bash
$ applypilot agent answer --question currency --choice USD
```

```json
{"phase":"interview","mode":"choices","backend":"host","current_question":{"id":"salary_band","prompt":"What's the minimum annual salary you'd accept?","choices":[{"value":"60000","label":"$60k \u2013 $80k"},{"value":"80000","label":"$80k \u2013 $100k"},{"value":"100000","label":"$100k \u2013 $120k"},{"value":"120000","label":"$120k+"}],"prefilled":null,"allow_free_text":true,"free_text_kind":"number"},"prefill_chips":[],"last_agent_reply":null,"needs":{"resume":false,"confirm":false,"provider_setup":true},"progress":{"answered":9,"remaining":13},"resume":{"provided":true,"chars":133,"pending":true}}
```

```bash
$ applypilot agent next
```

```json
{"phase":"interview","mode":"choices","backend":"host","current_question":{"id":"salary_band","prompt":"What's the minimum annual salary you'd accept?","choices":[{"value":"60000","label":"$60k \u2013 $80k"},{"value":"80000","label":"$80k \u2013 $100k"},{"value":"100000","label":"$100k \u2013 $120k"},{"value":"120000","label":"$120k+"}],"prefilled":null,"allow_free_text":true,"free_text_kind":"number"},"prefill_chips":[],"last_agent_reply":null,"needs":{"resume":false,"confirm":false,"provider_setup":true},"progress":{"answered":9,"remaining":13},"resume":{"provided":true,"chars":133,"pending":true}}
```

```bash
$ applypilot agent answer --question salary_band --choice 60000
```

```json
{"phase":"interview","mode":"choices","backend":"host","current_question":{"id":"start_date","prompt":"When could you start?","choices":[{"value":"Immediately","label":"Immediately"},{"value":"In 2 weeks","label":"In 2 weeks"},{"value":"In 1 month","label":"In 1 month"},{"value":"In 3 months","label":"In 3 months"}],"prefilled":null,"allow_free_text":true,"free_text_kind":"date"},"prefill_chips":[],"last_agent_reply":null,"needs":{"resume":false,"confirm":false,"provider_setup":true},"progress":{"answered":10,"remaining":12},"resume":{"provided":true,"chars":133,"pending":true}}
```

```bash
$ applypilot agent next
```

```json
{"phase":"interview","mode":"choices","backend":"host","current_question":{"id":"start_date","prompt":"When could you start?","choices":[{"value":"Immediately","label":"Immediately"},{"value":"In 2 weeks","label":"In 2 weeks"},{"value":"In 1 month","label":"In 1 month"},{"value":"In 3 months","label":"In 3 months"}],"prefilled":null,"allow_free_text":true,"free_text_kind":"date"},"prefill_chips":[],"last_agent_reply":null,"needs":{"resume":false,"confirm":false,"provider_setup":true},"progress":{"answered":10,"remaining":12},"resume":{"provided":true,"chars":133,"pending":true}}
```

```bash
$ applypilot agent answer --question start_date --choice Immediately
```

```json
{"phase":"interview","mode":"choices","backend":"host","current_question":{"id":"target_roles","prompt":"Which job titles should we search for? One per line.","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":"multiline"},"prefill_chips":[],"last_agent_reply":null,"needs":{"resume":false,"confirm":false,"provider_setup":true},"progress":{"answered":11,"remaining":11},"resume":{"provided":true,"chars":133,"pending":true}}
```

```bash
$ applypilot agent next
```

```json
{"phase":"interview","mode":"choices","backend":"host","current_question":{"id":"target_roles","prompt":"Which job titles should we search for? One per line.","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":"multiline"},"prefill_chips":[],"last_agent_reply":null,"needs":{"resume":false,"confirm":false,"provider_setup":true},"progress":{"answered":11,"remaining":11},"resume":{"provided":true,"chars":133,"pending":true}}
```

```bash
$ applypilot agent answer --question target_roles --free-text "Something reasonable"
```

```json
{"phase":"interview","mode":"choices","backend":"host","current_question":{"id":"skills_languages","prompt":"Which programming languages do you use?","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":"multiline"},"prefill_chips":[],"last_agent_reply":null,"needs":{"resume":false,"confirm":false,"provider_setup":true},"progress":{"answered":12,"remaining":10},"resume":{"provided":true,"chars":133,"pending":true}}
```

```bash
$ applypilot agent next
```

```json
{"phase":"interview","mode":"choices","backend":"host","current_question":{"id":"skills_languages","prompt":"Which programming languages do you use?","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":"multiline"},"prefill_chips":[],"last_agent_reply":null,"needs":{"resume":false,"confirm":false,"provider_setup":true},"progress":{"answered":12,"remaining":10},"resume":{"provided":true,"chars":133,"pending":true}}
```

```bash
$ applypilot agent answer --question skills_languages --free-text "Something reasonable"
```

```json
{"phase":"interview","mode":"choices","backend":"host","current_question":{"id":"skills_frameworks","prompt":"Which frameworks or libraries do you use?","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":"multiline"},"prefill_chips":[],"last_agent_reply":null,"needs":{"resume":false,"confirm":false,"provider_setup":true},"progress":{"answered":13,"remaining":9},"resume":{"provided":true,"chars":133,"pending":true}}
```

```bash
$ applypilot agent next
```

```json
{"phase":"interview","mode":"choices","backend":"host","current_question":{"id":"skills_frameworks","prompt":"Which frameworks or libraries do you use?","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":"multiline"},"prefill_chips":[],"last_agent_reply":null,"needs":{"resume":false,"confirm":false,"provider_setup":true},"progress":{"answered":13,"remaining":9},"resume":{"provided":true,"chars":133,"pending":true}}
```

```bash
$ applypilot agent answer --question skills_frameworks --free-text "Something reasonable"
```

```json
{"phase":"interview","mode":"choices","backend":"host","current_question":{"id":"skills_tools","prompt":"Which tools do you work with?","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":"multiline"},"prefill_chips":[],"last_agent_reply":null,"needs":{"resume":false,"confirm":false,"provider_setup":true},"progress":{"answered":14,"remaining":8},"resume":{"provided":true,"chars":133,"pending":true}}
```

```bash
$ applypilot agent next
```

```json
{"phase":"interview","mode":"choices","backend":"host","current_question":{"id":"skills_tools","prompt":"Which tools do you work with?","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":"multiline"},"prefill_chips":[],"last_agent_reply":null,"needs":{"resume":false,"confirm":false,"provider_setup":true},"progress":{"answered":14,"remaining":8},"resume":{"provided":true,"chars":133,"pending":true}}
```

```bash
$ applypilot agent answer --question skills_tools --free-text "Something reasonable"
```

```json
{"phase":"interview","mode":"choices","backend":"host","current_question":{"id":"email","prompt":"What email address should applications use?","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[],"last_agent_reply":null,"needs":{"resume":false,"confirm":false,"provider_setup":true},"progress":{"answered":15,"remaining":7},"resume":{"provided":true,"chars":133,"pending":true}}
```

```bash
$ applypilot agent next
```

```json
{"phase":"interview","mode":"choices","backend":"host","current_question":{"id":"email","prompt":"What email address should applications use?","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[],"last_agent_reply":null,"needs":{"resume":false,"confirm":false,"provider_setup":true},"progress":{"answered":15,"remaining":7},"resume":{"provided":true,"chars":133,"pending":true}}
```

```bash
$ applypilot agent answer --question email --free-text "Something reasonable"
```

```json
{"phase":"interview","mode":"choices","backend":"host","current_question":{"id":"phone","prompt":"What phone number should applications use?","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[],"last_agent_reply":null,"needs":{"resume":false,"confirm":false,"provider_setup":true},"progress":{"answered":16,"remaining":6},"resume":{"provided":true,"chars":133,"pending":true}}
```

```bash
$ applypilot agent next
```

```json
{"phase":"interview","mode":"choices","backend":"host","current_question":{"id":"phone","prompt":"What phone number should applications use?","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[],"last_agent_reply":null,"needs":{"resume":false,"confirm":false,"provider_setup":true},"progress":{"answered":16,"remaining":6},"resume":{"provided":true,"chars":133,"pending":true}}
```

```bash
$ applypilot agent answer --question phone --free-text "Something reasonable"
```

```json
{"phase":"interview","mode":"choices","backend":"host","current_question":{"id":"city","prompt":"What city do you live in?","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[],"last_agent_reply":null,"needs":{"resume":false,"confirm":false,"provider_setup":true},"progress":{"answered":17,"remaining":5},"resume":{"provided":true,"chars":133,"pending":true}}
```

```bash
$ applypilot agent next
```

```json
{"phase":"interview","mode":"choices","backend":"host","current_question":{"id":"city","prompt":"What city do you live in?","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[],"last_agent_reply":null,"needs":{"resume":false,"confirm":false,"provider_setup":true},"progress":{"answered":17,"remaining":5},"resume":{"provided":true,"chars":133,"pending":true}}
```

```bash
$ applypilot agent answer --question city --free-text "Something reasonable"
```

```json
{"phase":"interview","mode":"choices","backend":"host","current_question":{"id":"linkedin_url","prompt":"Link your LinkedIn profile (optional)","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[],"last_agent_reply":null,"needs":{"resume":false,"confirm":false,"provider_setup":true},"progress":{"answered":18,"remaining":4},"resume":{"provided":true,"chars":133,"pending":true}}
```

```bash
$ applypilot agent next
```

```json
{"phase":"interview","mode":"choices","backend":"host","current_question":{"id":"linkedin_url","prompt":"Link your LinkedIn profile (optional)","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[],"last_agent_reply":null,"needs":{"resume":false,"confirm":false,"provider_setup":true},"progress":{"answered":18,"remaining":4},"resume":{"provided":true,"chars":133,"pending":true}}
```

```bash
$ applypilot agent answer --question linkedin_url --free-text "Something reasonable"
```

```json
{"phase":"interview","mode":"choices","backend":"host","current_question":{"id":"github_url","prompt":"Link your GitHub profile (optional)","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[],"last_agent_reply":null,"needs":{"resume":false,"confirm":false,"provider_setup":true},"progress":{"answered":19,"remaining":3},"resume":{"provided":true,"chars":133,"pending":true}}
```

```bash
$ applypilot agent next
```

```json
{"phase":"interview","mode":"choices","backend":"host","current_question":{"id":"github_url","prompt":"Link your GitHub profile (optional)","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[],"last_agent_reply":null,"needs":{"resume":false,"confirm":false,"provider_setup":true},"progress":{"answered":19,"remaining":3},"resume":{"provided":true,"chars":133,"pending":true}}
```

```bash
$ applypilot agent answer --question github_url --free-text "Something reasonable"
```

```json
{"phase":"interview","mode":"choices","backend":"host","current_question":{"id":"website_url","prompt":"Link a personal website (optional)","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[],"last_agent_reply":null,"needs":{"resume":false,"confirm":false,"provider_setup":true},"progress":{"answered":20,"remaining":2},"resume":{"provided":true,"chars":133,"pending":true}}
```

```bash
$ applypilot agent next
```

```json
{"phase":"interview","mode":"choices","backend":"host","current_question":{"id":"website_url","prompt":"Link a personal website (optional)","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[],"last_agent_reply":null,"needs":{"resume":false,"confirm":false,"provider_setup":true},"progress":{"answered":20,"remaining":2},"resume":{"provided":true,"chars":133,"pending":true}}
```

```bash
$ applypilot agent answer --question website_url --free-text "Something reasonable"
```

```json
{"phase":"interview","mode":"choices","backend":"host","current_question":{"id":"portfolio_url","prompt":"Link a portfolio site (optional)","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[],"last_agent_reply":null,"needs":{"resume":false,"confirm":false,"provider_setup":true},"progress":{"answered":21,"remaining":1},"resume":{"provided":true,"chars":133,"pending":true}}
```

```bash
$ applypilot agent next
```

```json
{"phase":"interview","mode":"choices","backend":"host","current_question":{"id":"portfolio_url","prompt":"Link a portfolio site (optional)","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[],"last_agent_reply":null,"needs":{"resume":false,"confirm":false,"provider_setup":true},"progress":{"answered":21,"remaining":1},"resume":{"provided":true,"chars":133,"pending":true}}
```

```bash
$ applypilot agent answer --question portfolio_url --free-text "Something reasonable"
```

```json
{"phase":"review","mode":"choices","backend":"host","current_question":null,"prefill_chips":[{"question_id":"location","prompt":"What city are you based in?","value":"Something reasonable","choices":[],"allow_free_text":true}],"last_agent_reply":null,"needs":{"resume":false,"confirm":true,"provider_setup":true},"progress":{"answered":22,"remaining":0},"resume":{"provided":true,"chars":133,"pending":true},"review_fields":[{"id":"full_name","prompt":"What's your full name?","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"education","prompt":"What's your highest education?","value":"Bachelor's","choices":[{"value":"Bachelor's","label":"Bachelor's"},{"value":"Master's","label":"Master's"},{"value":"PhD","label":"PhD"},{"value":"Self-taught","label":"Self-taught"}],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"experience_years","prompt":"How many years have you worked in your field?","value":"0\u20132","choices":[{"value":"0\u20132","label":"0\u20132"},{"value":"3\u20135","label":"3\u20135"},{"value":"6\u201310","label":"6\u201310"},{"value":"10+","label":"10+"}],"allow_free_text":false,"from_resume":false,"needs_answer":false},{"id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false,"from_resume":false,"needs_answer":false},{"id":"country","prompt":"Which country will you be working from / applying to jobs in?","value":"United States","choices":[{"value":"United States","label":"United States"},{"value":"United Kingdom","label":"United Kingdom"},{"value":"Canada","label":"Canada"},{"value":"European Union","label":"European Union"},{"value":"India","label":"India"},{"value":"Other","label":"Other"},{"value":"Not sure yet","label":"Not sure yet"}],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"work_authorized","prompt":"Are you legally authorized to work in the country you're applying in?","value":"yes","choices":[{"value":"yes","label":"Yes"},{"value":"no","label":"No"}],"allow_free_text":false,"from_resume":false,"needs_answer":false},{"id":"sponsorship","prompt":"Will you now or in the future need visa sponsorship?","value":"yes","choices":[{"value":"yes","label":"Yes"},{"value":"no","label":"No"},{"value":"not_sure","label":"Not sure"}],"allow_free_text":false,"from_resume":false,"needs_answer":false},{"id":"permit_type","prompt":"Which best describes your right to work?","value":"Citizen","choices":[{"value":"Citizen","label":"Citizen"},{"value":"Permanent resident","label":"Permanent resident"},{"value":"Open work permit","label":"Open work permit"},{"value":"H-1B","label":"H-1B"}],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"currency","prompt":"Which currency should salary conversations use?","value":"USD","choices":[{"value":"USD","label":"USD"},{"value":"EUR","label":"EUR"},{"value":"GBP","label":"GBP"},{"value":"CAD","label":"CAD"},{"value":"INR","label":"INR"}],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"salary_band","prompt":"What's the minimum annual salary you'd accept?","value":"60000","choices":[{"value":"60000","label":"$60k \u2013 $80k"},{"value":"80000","label":"$80k \u2013 $100k"},{"value":"100000","label":"$100k \u2013 $120k"},{"value":"120000","label":"$120k+"}],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"start_date","prompt":"When could you start?","value":"Immediately","choices":[{"value":"Immediately","label":"Immediately"},{"value":"In 2 weeks","label":"In 2 weeks"},{"value":"In 1 month","label":"In 1 month"},{"value":"In 3 months","label":"In 3 months"}],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"location","prompt":"What city are you based in?","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":true,"needs_answer":false},{"id":"skills_languages","prompt":"Which programming languages do you use?","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"skills_frameworks","prompt":"Which frameworks or libraries do you use?","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"skills_tools","prompt":"Which tools do you work with?","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"email","prompt":"What email address should applications use?","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"phone","prompt":"What phone number should applications use?","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"city","prompt":"What city do you live in?","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"linkedin_url","prompt":"Link your LinkedIn profile (optional)","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"github_url","prompt":"Link your GitHub profile (optional)","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"website_url","prompt":"Link a personal website (optional)","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"portfolio_url","prompt":"Link a portfolio site (optional)","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false}],"missing_review_fields":[]}
```

```bash
$ applypilot agent next
```

```json
{"phase":"review","mode":"choices","backend":"host","current_question":null,"prefill_chips":[{"question_id":"location","prompt":"What city are you based in?","value":"Something reasonable","choices":[],"allow_free_text":true}],"last_agent_reply":null,"needs":{"resume":false,"confirm":true,"provider_setup":true},"progress":{"answered":22,"remaining":0},"resume":{"provided":true,"chars":133,"pending":true},"review_fields":[{"id":"full_name","prompt":"What's your full name?","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"education","prompt":"What's your highest education?","value":"Bachelor's","choices":[{"value":"Bachelor's","label":"Bachelor's"},{"value":"Master's","label":"Master's"},{"value":"PhD","label":"PhD"},{"value":"Self-taught","label":"Self-taught"}],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"experience_years","prompt":"How many years have you worked in your field?","value":"0\u20132","choices":[{"value":"0\u20132","label":"0\u20132"},{"value":"3\u20135","label":"3\u20135"},{"value":"6\u201310","label":"6\u201310"},{"value":"10+","label":"10+"}],"allow_free_text":false,"from_resume":false,"needs_answer":false},{"id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false,"from_resume":false,"needs_answer":false},{"id":"country","prompt":"Which country will you be working from / applying to jobs in?","value":"United States","choices":[{"value":"United States","label":"United States"},{"value":"United Kingdom","label":"United Kingdom"},{"value":"Canada","label":"Canada"},{"value":"European Union","label":"European Union"},{"value":"India","label":"India"},{"value":"Other","label":"Other"},{"value":"Not sure yet","label":"Not sure yet"}],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"work_authorized","prompt":"Are you legally authorized to work in the country you're applying in?","value":"yes","choices":[{"value":"yes","label":"Yes"},{"value":"no","label":"No"}],"allow_free_text":false,"from_resume":false,"needs_answer":false},{"id":"sponsorship","prompt":"Will you now or in the future need visa sponsorship?","value":"yes","choices":[{"value":"yes","label":"Yes"},{"value":"no","label":"No"},{"value":"not_sure","label":"Not sure"}],"allow_free_text":false,"from_resume":false,"needs_answer":false},{"id":"permit_type","prompt":"Which best describes your right to work?","value":"Citizen","choices":[{"value":"Citizen","label":"Citizen"},{"value":"Permanent resident","label":"Permanent resident"},{"value":"Open work permit","label":"Open work permit"},{"value":"H-1B","label":"H-1B"}],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"currency","prompt":"Which currency should salary conversations use?","value":"USD","choices":[{"value":"USD","label":"USD"},{"value":"EUR","label":"EUR"},{"value":"GBP","label":"GBP"},{"value":"CAD","label":"CAD"},{"value":"INR","label":"INR"}],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"salary_band","prompt":"What's the minimum annual salary you'd accept?","value":"60000","choices":[{"value":"60000","label":"$60k \u2013 $80k"},{"value":"80000","label":"$80k \u2013 $100k"},{"value":"100000","label":"$100k \u2013 $120k"},{"value":"120000","label":"$120k+"}],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"start_date","prompt":"When could you start?","value":"Immediately","choices":[{"value":"Immediately","label":"Immediately"},{"value":"In 2 weeks","label":"In 2 weeks"},{"value":"In 1 month","label":"In 1 month"},{"value":"In 3 months","label":"In 3 months"}],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"location","prompt":"What city are you based in?","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":true,"needs_answer":false},{"id":"skills_languages","prompt":"Which programming languages do you use?","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"skills_frameworks","prompt":"Which frameworks or libraries do you use?","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"skills_tools","prompt":"Which tools do you work with?","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"email","prompt":"What email address should applications use?","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"phone","prompt":"What phone number should applications use?","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"city","prompt":"What city do you live in?","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"linkedin_url","prompt":"Link your LinkedIn profile (optional)","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"github_url","prompt":"Link your GitHub profile (optional)","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"website_url","prompt":"Link a personal website (optional)","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"portfolio_url","prompt":"Link a portfolio site (optional)","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false}],"missing_review_fields":[]}
```

```bash
$ applypilot agent confirm
```

```json
{
  "phase": "done",
  "mode": "choices",
  "backend": "host",
  "current_question": null,
  "prefill_chips": [
    {
      "question_id": "location",
      "prompt": "What city are you based in?",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true
    }
  ],
  "last_agent_reply": null,
  "needs": {
    "resume": false,
    "confirm": false,
    "provider_setup": true
  },
  "progress": {
    "answered": 22,
    "remaining": 0
  },
  "resume": {
    "provided": true,
    "chars": 133,
    "pending": true
  },
  "written": {
    "profile": "/home/user/work/dogfood-pipeline/applypilot/profile.json",
    "profile_md": "/home/user/work/dogfood-pipeline/applypilot/profile.md",
    "searches_yaml": "/home/user/work/dogfood-pipeline/applypilot/searches.yaml"
  }
}
```

- PASS: journey onboarding confirms (host-written profile)

```bash
$ applypilot run discover
```

```json
╭──────────────────────────────────╮
│ ApplyPilot Pipeline (sequential) │
╰──────────────────────────────────╯
  Min score:  7
  Workers:    1
  Validation: normal
  Stages:     discover
  DB:        2 jobs, 0 pending enrichment

======================================================================
  STAGE: discover — Job discovery (JobSpy + Workday + smart extract + Composio)
  Started: 00:27:16
======================================================================
  JobSpy full crawl...
  Workday corporate scraper...
  Smart extract (AI-powered scraping)...
  Composio (web3.career)...

  Stage 'discover' completed in 0.0s — ok

======================================================================
      Pipeline Summary      
┏━━━━━━━━━━┳━━━━━━━━┳━━━━━━┓
┃ Stage    ┃ Status ┃ Time ┃
┡━━━━━━━━━━╇━━━━━━━━╇━━━━━━┩
│ discover │ ok     │ 0.0s │
│          │        │      │
│ Total    │        │ 0.0s │
└──────────┴────────┴──────┘

  DB Final State:
    Total jobs:     5
    With desc:      2
    Scored:         2
    Tailored:       2
    Cover letters:  2
    Ready to apply: 1
    Applied:        1
======================================================================
```

```bash
$ applypilot run enrich
```

```json
╭──────────────────────────────────╮
│ ApplyPilot Pipeline (sequential) │
╰──────────────────────────────────╯
  Min score:  7
  Workers:    1
  Validation: normal
  Stages:     enrich
  DB:        5 jobs, 3 pending enrichment

======================================================================
  STAGE: enrich — Detail enrichment (full descriptions + apply URLs)
  Started: 00:27:16
======================================================================

  Stage 'enrich' completed in 0.0s — ok

======================================================================
     Pipeline Summary     
┏━━━━━━━━┳━━━━━━━━┳━━━━━━┓
┃ Stage  ┃ Status ┃ Time ┃
┡━━━━━━━━╇━━━━━━━━╇━━━━━━┩
│ enrich │ ok     │ 0.0s │
│        │        │      │
│ Total  │        │ 0.0s │
└────────┴────────┴──────┘

  DB Final State:
    Total jobs:     5
    With desc:      4
    Scored:         2
    Tailored:       2
    Cover letters:  2
    Ready to apply: 1
    Applied:        1
======================================================================
```

```bash
$ applypilot agent job-next --stage score
```

```json
{
  "stage": "score",
  "job": {
    "url": "https://jobs.example/gamma",
    "title": "Data Engineer",
    "site": "exampleboard",
    "location": null,
    "salary": null,
    "fit_score": null,
    "description_excerpt": "Full posting for Data Engineer: Python, SQL, Airflow, dbt.",
    "description_chars": 58,
    "excerpt_truncated": false
  },
  "progress": {
    "total": 5,
    "with_description": 4,
    "scored": 2,
    "unscored": 2,
    "score_distribution": [
      [
        9,
        2
      ]
    ],
    "tailored": 2,
    "untailored_eligible": 0,
    "tailor_exhausted": 0,
    "with_cover_letter": 2,
    "cover_exhausted": 0,
    "pending_detail": 1,
    "detail_errors": 0
  },
  "resume": {
    "provided": true,
    "chars": 133
  },
  "rubric": "You are a job fit evaluator. Given a candidate's resume and a job description, score how well the candidate fits the role.\n\nSCORING CRITERIA:\n- 9-10: Perfect match. Candidate has direct experience in nearly all required skills and qualifications.\n- 7-8: Strong match. Candidate has most required skills, minor gaps easily bridged.\n- 5-6: Moderate match. Candidate has some relevant skills but missing key requirements.\n- 3-4: Weak match. Significant skill gaps, would need substantial ramp-up.\n- 1-2: Poor match. Completely different field or experience level.\n\nIMPORTANT FACTORS:\n- Weight technical skills heavily (programming languages, frameworks, tools)\n- Consider transferable experience (automation, scripting, API work)\n- Factor in the candidate's project experience\n- Be realistic about experience level vs. job requirements (years of experience, seniority)\n\nRESPOND IN EXACTLY THIS FORMAT (no other text):\nSCORE: [1-10]\nKEYWORDS: [comma-separated ATS keywords from the job description that match or could match the candidate]\nREASONING: [2-3 sentences explaining the score]"
}
```

- PASS: job-next carries the 1-10 rubric

- PASS: default payload carries no resume text

- PASS: straggler visible: pending_detail == 1 — {"total": 5, "with_description": 4, "scored": 2, "unscored": 2, "score_distribution": [[9, 2]], "tailored": 2, "untailored_eligible": 0, "tailor_exhausted": 0, "with_cover_letter": 2, "cover_exhausted": 0, "pending_detail": 1, "detail_errors": 0}

```bash
$ applypilot agent job-next --stage score
```

```json
{"stage":"score","job":{"url":"https://jobs.example/gamma","title":"Data Engineer","site":"exampleboard","location":null,"salary":null,"fit_score":null,"description_excerpt":"Full posting for Data Engineer: Python, SQL, Airflow, dbt.","description_chars":58,"excerpt_truncated":false},"progress":{"total":5,"with_description":4,"scored":2,"unscored":2,"score_distribution":[[9,2]],"tailored":2,"untailored_eligible":0,"tailor_exhausted":0,"with_cover_letter":2,"cover_exhausted":0,"pending_detail":1,"detail_errors":0},"resume":{"provided":true,"chars":133},"rubric":"You are a job fit evaluator. Given a candidate's resume and a job description, score how well the candidate fits the role.\n\nSCORING CRITERIA:\n- 9-10: Perfect match. Candidate has direct experience in nearly all required skills and qualifications.\n- 7-8: Strong match. Candidate has most required skills, minor gaps easily bridged.\n- 5-6: Moderate match. Candidate has some relevant skills but missing key requirements.\n- 3-4: Weak match. Significant skill gaps, would need substantial ramp-up.\n- 1-2: Poor match. Completely different field or experience level.\n\nIMPORTANT FACTORS:\n- Weight technical skills heavily (programming languages, frameworks, tools)\n- Consider transferable experience (automation, scripting, API work)\n- Factor in the candidate's project experience\n- Be realistic about experience level vs. job requirements (years of experience, seniority)\n\nRESPOND IN EXACTLY THIS FORMAT (no other text):\nSCORE: [1-10]\nKEYWORDS: [comma-separated ATS keywords from the job description that match or could match the candidate]\nREASONING: [2-3 sentences explaining the score]"}
```

- scoring `https://jobs.example/gamma` (2 unscored)

```bash
$ applypilot agent score --url https://jobs.example/gamma --score 9 --keywords "python, sql, airflow" --reasoning "Host journey judgment: the posting's pipeline skills match the profile directly."
```

```json
{"stage":"score","job":{"url":"https://jobs.example/epsilon","title":"Data Engineer","site":"exampleboard","location":null,"salary":null,"fit_score":null,"description_excerpt":"Full posting for Data Engineer: Python, SQL, Airflow, dbt.","description_chars":58,"excerpt_truncated":false},"progress":{"total":5,"with_description":4,"scored":3,"unscored":1,"score_distribution":[[9,3]],"tailored":2,"untailored_eligible":1,"tailor_exhausted":0,"with_cover_letter":2,"cover_exhausted":0,"pending_detail":1,"detail_errors":0},"resume":{"provided":true,"chars":133},"rubric":"You are a job fit evaluator. Given a candidate's resume and a job description, score how well the candidate fits the role.\n\nSCORING CRITERIA:\n- 9-10: Perfect match. Candidate has direct experience in nearly all required skills and qualifications.\n- 7-8: Strong match. Candidate has most required skills, minor gaps easily bridged.\n- 5-6: Moderate match. Candidate has some relevant skills but missing key requirements.\n- 3-4: Weak match. Significant skill gaps, would need substantial ramp-up.\n- 1-2: Poor match. Completely different field or experience level.\n\nIMPORTANT FACTORS:\n- Weight technical skills heavily (programming languages, frameworks, tools)\n- Consider transferable experience (automation, scripting, API work)\n- Factor in the candidate's project experience\n- Be realistic about experience level vs. job requirements (years of experience, seniority)\n\nRESPOND IN EXACTLY THIS FORMAT (no other text):\nSCORE: [1-10]\nKEYWORDS: [comma-separated ATS keywords from the job description that match or could match the candidate]\nREASONING: [2-3 sentences explaining the score]"}
```

```bash
$ applypilot agent job-next --stage score
```

```json
{"stage":"score","job":{"url":"https://jobs.example/epsilon","title":"Data Engineer","site":"exampleboard","location":null,"salary":null,"fit_score":null,"description_excerpt":"Full posting for Data Engineer: Python, SQL, Airflow, dbt.","description_chars":58,"excerpt_truncated":false},"progress":{"total":5,"with_description":4,"scored":3,"unscored":1,"score_distribution":[[9,3]],"tailored":2,"untailored_eligible":1,"tailor_exhausted":0,"with_cover_letter":2,"cover_exhausted":0,"pending_detail":1,"detail_errors":0},"resume":{"provided":true,"chars":133},"rubric":"You are a job fit evaluator. Given a candidate's resume and a job description, score how well the candidate fits the role.\n\nSCORING CRITERIA:\n- 9-10: Perfect match. Candidate has direct experience in nearly all required skills and qualifications.\n- 7-8: Strong match. Candidate has most required skills, minor gaps easily bridged.\n- 5-6: Moderate match. Candidate has some relevant skills but missing key requirements.\n- 3-4: Weak match. Significant skill gaps, would need substantial ramp-up.\n- 1-2: Poor match. Completely different field or experience level.\n\nIMPORTANT FACTORS:\n- Weight technical skills heavily (programming languages, frameworks, tools)\n- Consider transferable experience (automation, scripting, API work)\n- Factor in the candidate's project experience\n- Be realistic about experience level vs. job requirements (years of experience, seniority)\n\nRESPOND IN EXACTLY THIS FORMAT (no other text):\nSCORE: [1-10]\nKEYWORDS: [comma-separated ATS keywords from the job description that match or could match the candidate]\nREASONING: [2-3 sentences explaining the score]"}
```

- scoring `https://jobs.example/epsilon` (1 unscored)

```bash
$ applypilot agent score --url https://jobs.example/epsilon --score 9 --keywords "python, sql, airflow" --reasoning "Host journey judgment: the posting's pipeline skills match the profile directly."
```

```json
{"stage":"score","job":null,"progress":{"total":5,"with_description":4,"scored":4,"unscored":0,"score_distribution":[[9,4]],"tailored":2,"untailored_eligible":2,"tailor_exhausted":0,"with_cover_letter":2,"cover_exhausted":0,"pending_detail":1,"detail_errors":0},"resume":{"provided":true,"chars":133},"rubric":"You are a job fit evaluator. Given a candidate's resume and a job description, score how well the candidate fits the role.\n\nSCORING CRITERIA:\n- 9-10: Perfect match. Candidate has direct experience in nearly all required skills and qualifications.\n- 7-8: Strong match. Candidate has most required skills, minor gaps easily bridged.\n- 5-6: Moderate match. Candidate has some relevant skills but missing key requirements.\n- 3-4: Weak match. Significant skill gaps, would need substantial ramp-up.\n- 1-2: Poor match. Completely different field or experience level.\n\nIMPORTANT FACTORS:\n- Weight technical skills heavily (programming languages, frameworks, tools)\n- Consider transferable experience (automation, scripting, API work)\n- Factor in the candidate's project experience\n- Be realistic about experience level vs. job requirements (years of experience, seniority)\n\nRESPOND IN EXACTLY THIS FORMAT (no other text):\nSCORE: [1-10]\nKEYWORDS: [comma-separated ATS keywords from the job description that match or could match the candidate]\nREASONING: [2-3 sentences explaining the score]"}
```

```bash
$ applypilot agent job-next --stage score
```

```json
{"stage":"score","job":null,"progress":{"total":5,"with_description":4,"scored":4,"unscored":0,"score_distribution":[[9,4]],"tailored":2,"untailored_eligible":2,"tailor_exhausted":0,"with_cover_letter":2,"cover_exhausted":0,"pending_detail":1,"detail_errors":0},"resume":{"provided":true,"chars":133},"rubric":"You are a job fit evaluator. Given a candidate's resume and a job description, score how well the candidate fits the role.\n\nSCORING CRITERIA:\n- 9-10: Perfect match. Candidate has direct experience in nearly all required skills and qualifications.\n- 7-8: Strong match. Candidate has most required skills, minor gaps easily bridged.\n- 5-6: Moderate match. Candidate has some relevant skills but missing key requirements.\n- 3-4: Weak match. Significant skill gaps, would need substantial ramp-up.\n- 1-2: Poor match. Completely different field or experience level.\n\nIMPORTANT FACTORS:\n- Weight technical skills heavily (programming languages, frameworks, tools)\n- Consider transferable experience (automation, scripting, API work)\n- Factor in the candidate's project experience\n- Be realistic about experience level vs. job requirements (years of experience, seniority)\n\nRESPOND IN EXACTLY THIS FORMAT (no other text):\nSCORE: [1-10]\nKEYWORDS: [comma-separated ATS keywords from the job description that match or could match the candidate]\nREASONING: [2-3 sentences explaining the score]"}
```

- PASS: score loop terminated on unscored == 0

- PASS: two queue jobs scored through the host path — scored 2

```bash
$ applypilot agent score --url https://jobs.example/gamma --score 11 --reasoning "out of range"
```

```
(exit 1)
{
  "error": {
    "code": "invalid_score",
    "message": "--score must be 1-10 (the rubric's scale), got 11",
    "details": {
      "score": "11"
    }
  }
}
```

- PASS: refusal envelope code == invalid_score — got 'invalid_score'

```bash
$ applypilot agent score --url https://jobs.example/gamma --score 9 --reasoning "double-score without --rescore"
```

```
(exit 1)
{
  "error": {
    "code": "already_scored",
    "message": "https://jobs.example/gamma already carries a fit score \u2014 pass --rescore to overwrite it",
    "details": {
      "url": "https://jobs.example/gamma",
      "fit_score": 9
    }
  }
}
```

- PASS: refusal envelope code == already_scored — got 'already_scored'

```bash
$ applypilot agent job-next --stage enrich
```

```json
{
  "stage": "enrich",
  "job": {
    "url": "https://jobs.example/delta",
    "title": "Analytics Engineer",
    "site": "exampleboard",
    "location": null,
    "salary": null,
    "fit_score": null,
    "description_excerpt": "",
    "description_chars": 0,
    "excerpt_truncated": false
  },
  "progress": {
    "total": 5,
    "with_description": 4,
    "scored": 4,
    "unscored": 0,
    "score_distribution": [
      [
        9,
        4
      ]
    ],
    "tailored": 2,
    "untailored_eligible": 2,
    "tailor_exhausted": 0,
    "with_cover_letter": 2,
    "cover_exhausted": 0,
    "pending_detail": 1,
    "detail_errors": 0
  },
  "resume": {
    "provided": true,
    "chars": 133
  },
  "instructions": "Capture the job's full description text (responsibilities, requirements, every section) and, when the posting has one, the apply-button URL. Pass the description with --full-description; the verb runs the same persistence write as the enrichment fallback tier and moves the job into the score queue."
}
```

- PASS: enrich queue serves the stranded job — https://jobs.example/delta

```bash
$ applypilot agent enrich --url https://jobs.example/delta --full-description "Full posting for Analytics Engineer: Python, SQL, Airflow, dbt." --application-url https://jobs.example/delta
```

```json
{"stage":"enrich","job":null,"progress":{"total":5,"with_description":5,"scored":4,"unscored":1,"score_distribution":[[9,4]],"tailored":2,"untailored_eligible":2,"tailor_exhausted":0,"with_cover_letter":2,"cover_exhausted":0,"pending_detail":0,"detail_errors":0},"resume":{"provided":true,"chars":133},"instructions":"Capture the job's full description text (responsibilities, requirements, every section) and, when the posting has one, the apply-button URL. Pass the description with --full-description; the verb runs the same persistence write as the enrichment fallback tier and moves the job into the score queue."}
```

```bash
$ applypilot agent score --url https://jobs.example/delta --score 8 --keywords "python, sql" --reasoning "Host journey judgment: close match, one gap in orchestration depth."
```

```json
{"stage":"score","job":null,"progress":{"total":5,"with_description":5,"scored":5,"unscored":0,"score_distribution":[[9,4],[8,1]],"tailored":2,"untailored_eligible":3,"tailor_exhausted":0,"with_cover_letter":2,"cover_exhausted":0,"pending_detail":0,"detail_errors":0},"resume":{"provided":true,"chars":133},"rubric":"You are a job fit evaluator. Given a candidate's resume and a job description, score how well the candidate fits the role.\n\nSCORING CRITERIA:\n- 9-10: Perfect match. Candidate has direct experience in nearly all required skills and qualifications.\n- 7-8: Strong match. Candidate has most required skills, minor gaps easily bridged.\n- 5-6: Moderate match. Candidate has some relevant skills but missing key requirements.\n- 3-4: Weak match. Significant skill gaps, would need substantial ramp-up.\n- 1-2: Poor match. Completely different field or experience level.\n\nIMPORTANT FACTORS:\n- Weight technical skills heavily (programming languages, frameworks, tools)\n- Consider transferable experience (automation, scripting, API work)\n- Factor in the candidate's project experience\n- Be realistic about experience level vs. job requirements (years of experience, seniority)\n\nRESPOND IN EXACTLY THIS FORMAT (no other text):\nSCORE: [1-10]\nKEYWORDS: [comma-separated ATS keywords from the job description that match or could match the candidate]\nREASONING: [2-3 sentences explaining the score]"}
```

```bash
$ applypilot agent job-next --stage score
```

```json
{
  "stage": "score",
  "job": null,
  "progress": {
    "total": 5,
    "with_description": 5,
    "scored": 5,
    "unscored": 0,
    "score_distribution": [
      [
        9,
        4
      ],
      [
        8,
        1
      ]
    ],
    "tailored": 2,
    "untailored_eligible": 3,
    "tailor_exhausted": 0,
    "with_cover_letter": 2,
    "cover_exhausted": 0,
    "pending_detail": 0,
    "detail_errors": 0
  },
  "resume": {
    "provided": true,
    "chars": 133
  },
  "rubric": "You are a job fit evaluator. Given a candidate's resume and a job description, score how well the candidate fits the role.\n\nSCORING CRITERIA:\n- 9-10: Perfect match. Candidate has direct experience in nearly all required skills and qualifications.\n- 7-8: Strong match. Candidate has most required skills, minor gaps easily bridged.\n- 5-6: Moderate match. Candidate has some relevant skills but missing key requirements.\n- 3-4: Weak match. Significant skill gaps, would need substantial ramp-up.\n- 1-2: Poor match. Completely different field or experience level.\n\nIMPORTANT FACTORS:\n- Weight technical skills heavily (programming languages, frameworks, tools)\n- Consider transferable experience (automation, scripting, API work)\n- Factor in the candidate's project experience\n- Be realistic about experience level vs. job requirements (years of experience, seniority)\n\nRESPOND IN EXACTLY THIS FORMAT (no other text):\nSCORE: [1-10]\nKEYWORDS: [comma-separated ATS keywords from the job description that match or could match the candidate]\nREASONING: [2-3 sentences explaining the score]"
}
```

- PASS: after enrich + score: unscored == 0 AND pending_detail == 0 — {"total": 5, "with_description": 5, "scored": 5, "unscored": 0, "score_distribution": [[9, 4], [8, 1]], "tailored": 2, "untailored_eligible": 3, "tailor_exhausted": 0, "with_cover_letter": 2, "cover_exhausted": 0, "pending_detail": 0, "detail_errors": 0}

```bash
$ applypilot agent tailor --url https://jobs.example/gamma --data "{"title": "Senior Data Engineer", "summary": "Data engineer with six years of Python and SQL pipeline work, most recently orchestrating Airflow workflows for analytics teams.", "skills": {"Languages": "Python, SQL, Rust, Go, Scala"}, "experience": [{"header": "Data Engineer, Example Co (2020 - present)", "subtitle": "Analytics platform team", "bullets": ["Built Python and SQL pipelines feeding analytics dashboards.", "Orchestrated nightly Airflow workflows for reporting."]}], "projects": [], "education": "B.S. Computer Science"}"
```

```
(exit 1)
{
  "error": {
    "code": "invalid_resume_data",
    "message": "the tailored resume data failed validation \u2014 fix the errors and retry (nothing was persisted)",
    "details": {
      "errors": [
        "Fabricated skill: 'rust'",
        "Fabricated skill: 'scala'"
      ],
      "warnings": [],
      "avoid_notes": [
        "Fabricated skill: 'rust'",
        "Fabricated skill: 'scala'"
      ],
      "retryable": true
    }
  }
}
```

- PASS: refusal envelope code == invalid_resume_data — got 'invalid_resume_data'

```bash
$ applypilot agent job-next --stage tailor
```

```json
{"stage":"tailor","job":{"url":"https://jobs.example/gamma","title":"Data Engineer","site":"exampleboard","location":null,"salary":null,"fit_score":9,"description_excerpt":"Full posting for Data Engineer: Python, SQL, Airflow, dbt.","description_chars":58,"excerpt_truncated":false},"progress":{"total":5,"with_description":5,"scored":5,"unscored":0,"score_distribution":[[9,4],[8,1]],"tailored":2,"untailored_eligible":3,"tailor_exhausted":0,"with_cover_letter":2,"cover_exhausted":0,"pending_detail":0,"detail_errors":0},"resume":{"provided":true,"chars":133},"data_template":{"title":"Role Title","summary":"2-3 tailored sentences.","skills":{"Languages":"comma-separated skills","Frameworks":"comma-separated skills","DevOps & Infra":"comma-separated skills","Databases":"comma-separated skills","Tools":"comma-separated skills"},"experience":[{"header":"Title at Company","subtitle":"Tech | Dates","bullets":["bullet 1","bullet 2","bullet 3","bullet 4"]}],"projects":[{"header":"Project Name - Description","subtitle":"Tech | Dates","bullets":["bullet 1","bullet 2"]}],"education":"Degree | School"},"validation":{"mode":"lenient","retryable_errors":true}}
```

- writing tailor materials for `https://jobs.example/gamma`

```bash
$ applypilot agent tailor --url https://jobs.example/gamma --data "{"title": "Senior Data Engineer", "summary": "Data engineer with six years of Python and SQL pipeline work, most recently orchestrating Airflow workflows for analytics teams.", "skills": {"Languages": "Python, SQL", "Orchestration": "Airflow", "Modeling": "dbt"}, "experience": [{"header": "Data Engineer, Example Co (2020 - present)", "subtitle": "Analytics platform team", "bullets": ["Built Python and SQL pipelines feeding analytics dashboards.", "Orchestrated nightly Airflow workflows for reporting."]}], "projects": [], "education": "B.S. Computer Science"}"
```

```json
{"stage":"tailor","job":{"url":"https://jobs.example/epsilon","title":"Data Engineer","site":"exampleboard","location":null,"salary":null,"fit_score":9,"description_excerpt":"Full posting for Data Engineer: Python, SQL, Airflow, dbt.","description_chars":58,"excerpt_truncated":false},"progress":{"total":5,"with_description":5,"scored":5,"unscored":0,"score_distribution":[[9,4],[8,1]],"tailored":3,"untailored_eligible":2,"tailor_exhausted":0,"with_cover_letter":2,"cover_exhausted":0,"pending_detail":0,"detail_errors":0},"resume":{"provided":true,"chars":133},"data_template":{"title":"Role Title","summary":"2-3 tailored sentences.","skills":{"Languages":"comma-separated skills","Frameworks":"comma-separated skills","DevOps & Infra":"comma-separated skills","Databases":"comma-separated skills","Tools":"comma-separated skills"},"experience":[{"header":"Title at Company","subtitle":"Tech | Dates","bullets":["bullet 1","bullet 2","bullet 3","bullet 4"]}],"projects":[{"header":"Project Name - Description","subtitle":"Tech | Dates","bullets":["bullet 1","bullet 2"]}],"education":"Degree | School"},"validation":{"mode":"lenient","retryable_errors":true},"written":{"path":"/home/user/work/dogfood-pipeline/applypilot/tailored_resumes/exampleboard_Data_Engineer_d6a22c79_v1.txt","pdf_path":"/home/user/work/dogfood-pipeline/applypilot/tailored_resumes/exampleboard_Data_Engineer_d6a22c79_v1.pdf","version_id":5}}
```

```bash
$ applypilot agent job-next --stage tailor
```

```json
{"stage":"tailor","job":{"url":"https://jobs.example/epsilon","title":"Data Engineer","site":"exampleboard","location":null,"salary":null,"fit_score":9,"description_excerpt":"Full posting for Data Engineer: Python, SQL, Airflow, dbt.","description_chars":58,"excerpt_truncated":false},"progress":{"total":5,"with_description":5,"scored":5,"unscored":0,"score_distribution":[[9,4],[8,1]],"tailored":3,"untailored_eligible":2,"tailor_exhausted":0,"with_cover_letter":2,"cover_exhausted":0,"pending_detail":0,"detail_errors":0},"resume":{"provided":true,"chars":133},"data_template":{"title":"Role Title","summary":"2-3 tailored sentences.","skills":{"Languages":"comma-separated skills","Frameworks":"comma-separated skills","DevOps & Infra":"comma-separated skills","Databases":"comma-separated skills","Tools":"comma-separated skills"},"experience":[{"header":"Title at Company","subtitle":"Tech | Dates","bullets":["bullet 1","bullet 2","bullet 3","bullet 4"]}],"projects":[{"header":"Project Name - Description","subtitle":"Tech | Dates","bullets":["bullet 1","bullet 2"]}],"education":"Degree | School"},"validation":{"mode":"lenient","retryable_errors":true}}
```

- writing tailor materials for `https://jobs.example/epsilon`

```bash
$ applypilot agent tailor --url https://jobs.example/epsilon --data "{"title": "Senior Data Engineer", "summary": "Data engineer with six years of Python and SQL pipeline work, most recently orchestrating Airflow workflows for analytics teams.", "skills": {"Languages": "Python, SQL", "Orchestration": "Airflow", "Modeling": "dbt"}, "experience": [{"header": "Data Engineer, Example Co (2020 - present)", "subtitle": "Analytics platform team", "bullets": ["Built Python and SQL pipelines feeding analytics dashboards.", "Orchestrated nightly Airflow workflows for reporting."]}], "projects": [], "education": "B.S. Computer Science"}"
```

```json
{"stage":"tailor","job":{"url":"https://jobs.example/delta","title":"Analytics Engineer","site":"exampleboard","location":null,"salary":null,"fit_score":8,"description_excerpt":"Full posting for Analytics Engineer: Python, SQL, Airflow, dbt.","description_chars":63,"excerpt_truncated":false},"progress":{"total":5,"with_description":5,"scored":5,"unscored":0,"score_distribution":[[9,4],[8,1]],"tailored":4,"untailored_eligible":1,"tailor_exhausted":0,"with_cover_letter":2,"cover_exhausted":0,"pending_detail":0,"detail_errors":0},"resume":{"provided":true,"chars":133},"data_template":{"title":"Role Title","summary":"2-3 tailored sentences.","skills":{"Languages":"comma-separated skills","Frameworks":"comma-separated skills","DevOps & Infra":"comma-separated skills","Databases":"comma-separated skills","Tools":"comma-separated skills"},"experience":[{"header":"Title at Company","subtitle":"Tech | Dates","bullets":["bullet 1","bullet 2","bullet 3","bullet 4"]}],"projects":[{"header":"Project Name - Description","subtitle":"Tech | Dates","bullets":["bullet 1","bullet 2"]}],"education":"Degree | School"},"validation":{"mode":"lenient","retryable_errors":true},"written":{"path":"/home/user/work/dogfood-pipeline/applypilot/tailored_resumes/exampleboard_Data_Engineer_3f9988f3_v1.txt","pdf_path":"/home/user/work/dogfood-pipeline/applypilot/tailored_resumes/exampleboard_Data_Engineer_3f9988f3_v1.pdf","version_id":6}}
```

```bash
$ applypilot agent job-next --stage tailor
```

```json
{"stage":"tailor","job":{"url":"https://jobs.example/delta","title":"Analytics Engineer","site":"exampleboard","location":null,"salary":null,"fit_score":8,"description_excerpt":"Full posting for Analytics Engineer: Python, SQL, Airflow, dbt.","description_chars":63,"excerpt_truncated":false},"progress":{"total":5,"with_description":5,"scored":5,"unscored":0,"score_distribution":[[9,4],[8,1]],"tailored":4,"untailored_eligible":1,"tailor_exhausted":0,"with_cover_letter":2,"cover_exhausted":0,"pending_detail":0,"detail_errors":0},"resume":{"provided":true,"chars":133},"data_template":{"title":"Role Title","summary":"2-3 tailored sentences.","skills":{"Languages":"comma-separated skills","Frameworks":"comma-separated skills","DevOps & Infra":"comma-separated skills","Databases":"comma-separated skills","Tools":"comma-separated skills"},"experience":[{"header":"Title at Company","subtitle":"Tech | Dates","bullets":["bullet 1","bullet 2","bullet 3","bullet 4"]}],"projects":[{"header":"Project Name - Description","subtitle":"Tech | Dates","bullets":["bullet 1","bullet 2"]}],"education":"Degree | School"},"validation":{"mode":"lenient","retryable_errors":true}}
```

- writing tailor materials for `https://jobs.example/delta`

```bash
$ applypilot agent tailor --url https://jobs.example/delta --data "{"title": "Senior Data Engineer", "summary": "Data engineer with six years of Python and SQL pipeline work, most recently orchestrating Airflow workflows for analytics teams.", "skills": {"Languages": "Python, SQL", "Orchestration": "Airflow", "Modeling": "dbt"}, "experience": [{"header": "Data Engineer, Example Co (2020 - present)", "subtitle": "Analytics platform team", "bullets": ["Built Python and SQL pipelines feeding analytics dashboards.", "Orchestrated nightly Airflow workflows for reporting."]}], "projects": [], "education": "B.S. Computer Science"}"
```

```json
{"stage":"tailor","job":null,"progress":{"total":5,"with_description":5,"scored":5,"unscored":0,"score_distribution":[[9,4],[8,1]],"tailored":5,"untailored_eligible":0,"tailor_exhausted":0,"with_cover_letter":2,"cover_exhausted":0,"pending_detail":0,"detail_errors":0},"resume":{"provided":true,"chars":133},"data_template":{"title":"Role Title","summary":"2-3 tailored sentences.","skills":{"Languages":"comma-separated skills","Frameworks":"comma-separated skills","DevOps & Infra":"comma-separated skills","Databases":"comma-separated skills","Tools":"comma-separated skills"},"experience":[{"header":"Title at Company","subtitle":"Tech | Dates","bullets":["bullet 1","bullet 2","bullet 3","bullet 4"]}],"projects":[{"header":"Project Name - Description","subtitle":"Tech | Dates","bullets":["bullet 1","bullet 2"]}],"education":"Degree | School"},"validation":{"mode":"lenient","retryable_errors":true},"written":{"path":"/home/user/work/dogfood-pipeline/applypilot/tailored_resumes/exampleboard_Analytics_Engineer_252f703c_v1.txt","pdf_path":"/home/user/work/dogfood-pipeline/applypilot/tailored_resumes/exampleboard_Analytics_Engineer_252f703c_v1.pdf","version_id":7}}
```

```bash
$ applypilot agent job-next --stage tailor
```

```json
{"stage":"tailor","job":null,"progress":{"total":5,"with_description":5,"scored":5,"unscored":0,"score_distribution":[[9,4],[8,1]],"tailored":5,"untailored_eligible":0,"tailor_exhausted":0,"with_cover_letter":2,"cover_exhausted":0,"pending_detail":0,"detail_errors":0},"resume":{"provided":true,"chars":133},"data_template":{"title":"Role Title","summary":"2-3 tailored sentences.","skills":{"Languages":"comma-separated skills","Frameworks":"comma-separated skills","DevOps & Infra":"comma-separated skills","Databases":"comma-separated skills","Tools":"comma-separated skills"},"experience":[{"header":"Title at Company","subtitle":"Tech | Dates","bullets":["bullet 1","bullet 2","bullet 3","bullet 4"]}],"projects":[{"header":"Project Name - Description","subtitle":"Tech | Dates","bullets":["bullet 1","bullet 2"]}],"education":"Degree | School"},"validation":{"mode":"lenient","retryable_errors":true}}
```

```bash
$ applypilot agent job-next --stage cover
```

```json
{"stage":"cover","job":{"url":"https://jobs.example/gamma","title":"Data Engineer","site":"exampleboard","location":null,"salary":null,"fit_score":9,"description_excerpt":"Full posting for Data Engineer: Python, SQL, Airflow, dbt.","description_chars":58,"excerpt_truncated":false},"progress":{"total":5,"with_description":5,"scored":5,"unscored":0,"score_distribution":[[9,4],[8,1]],"tailored":5,"untailored_eligible":0,"tailor_exhausted":0,"with_cover_letter":2,"cover_exhausted":0,"pending_detail":0,"detail_errors":0},"resume":{"provided":true,"chars":133},"requirements":"3 short paragraphs, under 250 words. Start with 'Dear Hiring Manager,' and end with the sign-off name. No em or en dashes (auto-fixed on receipt). No meta-commentary. Validation runs in lenient mode: banned filler words are ignored, but LLM self-talk and a missing 'Dear' opening are hard errors."}
```

- writing cover materials for `https://jobs.example/gamma`

```bash
$ applypilot agent cover --url https://jobs.example/gamma --text "Dear Hiring Manager,

The Data Engineer opening on exampleboard matches the work I do: Python
and SQL pipelines, orchestrated in Airflow, feeding analytics that people
act on. My last six years were spent building exactly those systems.

I would welcome the chance to talk about the role.

Sincerely,
Test Person
"
```

```json
{"stage":"cover","job":{"url":"https://jobs.example/epsilon","title":"Data Engineer","site":"exampleboard","location":null,"salary":null,"fit_score":9,"description_excerpt":"Full posting for Data Engineer: Python, SQL, Airflow, dbt.","description_chars":58,"excerpt_truncated":false},"progress":{"total":5,"with_description":5,"scored":5,"unscored":0,"score_distribution":[[9,4],[8,1]],"tailored":5,"untailored_eligible":0,"tailor_exhausted":0,"with_cover_letter":3,"cover_exhausted":0,"pending_detail":0,"detail_errors":0},"resume":{"provided":true,"chars":133},"requirements":"3 short paragraphs, under 250 words. Start with 'Dear Hiring Manager,' and end with the sign-off name. No em or en dashes (auto-fixed on receipt). No meta-commentary. Validation runs in lenient mode: banned filler words are ignored, but LLM self-talk and a missing 'Dear' opening are hard errors.","written":{"path":"/home/user/work/dogfood-pipeline/applypilot/cover_letters/exampleboard_Data_Engineer_d6a22c79_v1_CL.txt","pdf_path":"/home/user/work/dogfood-pipeline/applypilot/cover_letters/exampleboard_Data_Engineer_d6a22c79_v1_CL.pdf","version_id":8}}
```

```bash
$ applypilot agent job-next --stage cover
```

```json
{"stage":"cover","job":{"url":"https://jobs.example/epsilon","title":"Data Engineer","site":"exampleboard","location":null,"salary":null,"fit_score":9,"description_excerpt":"Full posting for Data Engineer: Python, SQL, Airflow, dbt.","description_chars":58,"excerpt_truncated":false},"progress":{"total":5,"with_description":5,"scored":5,"unscored":0,"score_distribution":[[9,4],[8,1]],"tailored":5,"untailored_eligible":0,"tailor_exhausted":0,"with_cover_letter":3,"cover_exhausted":0,"pending_detail":0,"detail_errors":0},"resume":{"provided":true,"chars":133},"requirements":"3 short paragraphs, under 250 words. Start with 'Dear Hiring Manager,' and end with the sign-off name. No em or en dashes (auto-fixed on receipt). No meta-commentary. Validation runs in lenient mode: banned filler words are ignored, but LLM self-talk and a missing 'Dear' opening are hard errors."}
```

- writing cover materials for `https://jobs.example/epsilon`

```bash
$ applypilot agent cover --url https://jobs.example/epsilon --text "Dear Hiring Manager,

The Data Engineer opening on exampleboard matches the work I do: Python
and SQL pipelines, orchestrated in Airflow, feeding analytics that people
act on. My last six years were spent building exactly those systems.

I would welcome the chance to talk about the role.

Sincerely,
Test Person
"
```

```json
{"stage":"cover","job":{"url":"https://jobs.example/delta","title":"Analytics Engineer","site":"exampleboard","location":null,"salary":null,"fit_score":8,"description_excerpt":"Full posting for Analytics Engineer: Python, SQL, Airflow, dbt.","description_chars":63,"excerpt_truncated":false},"progress":{"total":5,"with_description":5,"scored":5,"unscored":0,"score_distribution":[[9,4],[8,1]],"tailored":5,"untailored_eligible":0,"tailor_exhausted":0,"with_cover_letter":4,"cover_exhausted":0,"pending_detail":0,"detail_errors":0},"resume":{"provided":true,"chars":133},"requirements":"3 short paragraphs, under 250 words. Start with 'Dear Hiring Manager,' and end with the sign-off name. No em or en dashes (auto-fixed on receipt). No meta-commentary. Validation runs in lenient mode: banned filler words are ignored, but LLM self-talk and a missing 'Dear' opening are hard errors.","written":{"path":"/home/user/work/dogfood-pipeline/applypilot/cover_letters/exampleboard_Data_Engineer_3f9988f3_v1_CL.txt","pdf_path":"/home/user/work/dogfood-pipeline/applypilot/cover_letters/exampleboard_Data_Engineer_3f9988f3_v1_CL.pdf","version_id":9}}
```

```bash
$ applypilot agent job-next --stage cover
```

```json
{"stage":"cover","job":{"url":"https://jobs.example/delta","title":"Analytics Engineer","site":"exampleboard","location":null,"salary":null,"fit_score":8,"description_excerpt":"Full posting for Analytics Engineer: Python, SQL, Airflow, dbt.","description_chars":63,"excerpt_truncated":false},"progress":{"total":5,"with_description":5,"scored":5,"unscored":0,"score_distribution":[[9,4],[8,1]],"tailored":5,"untailored_eligible":0,"tailor_exhausted":0,"with_cover_letter":4,"cover_exhausted":0,"pending_detail":0,"detail_errors":0},"resume":{"provided":true,"chars":133},"requirements":"3 short paragraphs, under 250 words. Start with 'Dear Hiring Manager,' and end with the sign-off name. No em or en dashes (auto-fixed on receipt). No meta-commentary. Validation runs in lenient mode: banned filler words are ignored, but LLM self-talk and a missing 'Dear' opening are hard errors."}
```

- writing cover materials for `https://jobs.example/delta`

```bash
$ applypilot agent cover --url https://jobs.example/delta --text "Dear Hiring Manager,

The Data Engineer opening on exampleboard matches the work I do: Python
and SQL pipelines, orchestrated in Airflow, feeding analytics that people
act on. My last six years were spent building exactly those systems.

I would welcome the chance to talk about the role.

Sincerely,
Test Person
"
```

```json
{"stage":"cover","job":null,"progress":{"total":5,"with_description":5,"scored":5,"unscored":0,"score_distribution":[[9,4],[8,1]],"tailored":5,"untailored_eligible":0,"tailor_exhausted":0,"with_cover_letter":5,"cover_exhausted":0,"pending_detail":0,"detail_errors":0},"resume":{"provided":true,"chars":133},"requirements":"3 short paragraphs, under 250 words. Start with 'Dear Hiring Manager,' and end with the sign-off name. No em or en dashes (auto-fixed on receipt). No meta-commentary. Validation runs in lenient mode: banned filler words are ignored, but LLM self-talk and a missing 'Dear' opening are hard errors.","written":{"path":"/home/user/work/dogfood-pipeline/applypilot/cover_letters/exampleboard_Analytics_Engineer_252f703c_v1_CL.txt","pdf_path":"/home/user/work/dogfood-pipeline/applypilot/cover_letters/exampleboard_Analytics_Engineer_252f703c_v1_CL.pdf","version_id":10}}
```

```bash
$ applypilot agent job-next --stage cover
```

```json
{"stage":"cover","job":null,"progress":{"total":5,"with_description":5,"scored":5,"unscored":0,"score_distribution":[[9,4],[8,1]],"tailored":5,"untailored_eligible":0,"tailor_exhausted":0,"with_cover_letter":5,"cover_exhausted":0,"pending_detail":0,"detail_errors":0},"resume":{"provided":true,"chars":133},"requirements":"3 short paragraphs, under 250 words. Start with 'Dear Hiring Manager,' and end with the sign-off name. No em or en dashes (auto-fixed on receipt). No meta-commentary. Validation runs in lenient mode: banned filler words are ignored, but LLM self-talk and a missing 'Dear' opening are hard errors."}
```

```bash
$ applypilot agent job-next --stage cover
```

```json
{
  "stage": "cover",
  "job": null,
  "progress": {
    "total": 5,
    "with_description": 5,
    "scored": 5,
    "unscored": 0,
    "score_distribution": [
      [
        9,
        4
      ],
      [
        8,
        1
      ]
    ],
    "tailored": 5,
    "untailored_eligible": 0,
    "tailor_exhausted": 0,
    "with_cover_letter": 5,
    "cover_exhausted": 0,
    "pending_detail": 0,
    "detail_errors": 0
  },
  "resume": {
    "provided": true,
    "chars": 133
  },
  "requirements": "3 short paragraphs, under 250 words. Start with 'Dear Hiring Manager,' and end with the sign-off name. No em or en dashes (auto-fixed on receipt). No meta-commentary. Validation runs in lenient mode: banned filler words are ignored, but LLM self-talk and a missing 'Dear' opening are hard errors."
}
```

- PASS: tailor + cover queues drained

```bash
$ applypilot apply --plan --min-score 7 --limit 3
```

```json
{
  "plan_hash": "8c5a72cca91ba2c5785deb20bd5d52f72b868c5effa6283c9f51c633e8f6c402",
  "day": "2026-11-08",
  "commits_nothing": true,
  "batch_size": 3,
  "min_score": 7,
  "cap": 5,
  "cap_source": "default",
  "applied_today": 1,
  "headroom": 4,
  "estimate_basis": "apply_timeout=300s per job (the run's watchdog budget \u2014 an upper bound)",
  "jobs": [
    {
      "url": "https://jobs.example/epsilon",
      "application_url": "https://jobs.example/epsilon",
      "title": "Data Engineer",
      "site": "exampleboard",
      "fit_score": 9,
      "estimate_seconds": 300
    },
    {
      "url": "https://jobs.example/gamma",
      "application_url": "https://jobs.example/gamma",
      "title": "Data Engineer",
      "site": "exampleboard",
      "fit_score": 9,
      "estimate_seconds": 300
    },
    {
      "url": "https://jobs.example/delta",
      "application_url": "https://jobs.example/delta",
      "title": "Analytics Engineer",
      "site": "exampleboard",
      "fit_score": 8,
      "estimate_seconds": 300
    }
  ],
  "next": "applypilot apply --i-confirm 8c5a72cca91ba2c5785deb20bd5d52f72b868c5effa6283c9f51c633e8f6c402 (same flags as this --plan run, same UTC day)"
}
```

- PASS: journey plan batch == 3 — {"batch_size": 3, "cap": 5, "headroom": 4}

- PASS: plan headroom reflects the day's earlier application — headroom=4

```bash
$ applypilot agent apply-result --url https://jobs.example/gamma --status applied
```

```
(exit 1)
{
  "error": {
    "code": "not_acquired",
    "message": "https://jobs.example/gamma is not under an apply lock \u2014 acquire it with `agent apply-acquire` first; apply-result records outcomes for acquired jobs only",
    "details": {
      "url": "https://jobs.example/gamma",
      "apply_status": null
    }
  }
}
```

- PASS: refusal envelope code == not_acquired — got 'not_acquired'

```bash
$ applypilot agent apply-acquire
```

```json
{
  "stage": "apply",
  "job": {
    "url": "https://jobs.example/epsilon",
    "title": "Data Engineer",
    "site": "exampleboard",
    "location": null,
    "salary": null,
    "fit_score": 9,
    "description_excerpt": "Full posting for Data Engineer: Python, SQL, Airflow, dbt.",
    "description_chars": 58,
    "excerpt_truncated": false
  },
  "application_url": "https://jobs.example/epsilon",
  "resume_path": "/home/user/work/dogfood-pipeline/applypilot/tailored_resumes/exampleboard_Data_Engineer_3f9988f3_v1.txt",
  "cover_letter_path": "/home/user/work/dogfood-pipeline/applypilot/cover_letters/exampleboard_Data_Engineer_3f9988f3_v1_CL.txt",
  "acquired_by": "host-worker-0",
  "apply": {
    "cap": 5,
    "applied_today": 1,
    "headroom": 4
  },
  "progress": {
    "total": 5,
    "with_description": 5,
    "scored": 5,
    "unscored": 0,
    "score_distribution": [
      [
        9,
        4
      ],
      [
        8,
        1
      ]
    ],
    "tailored": 5,
    "untailored_eligible": 0,
    "tailor_exhausted": 0,
    "with_cover_letter": 5,
    "cover_exhausted": 0,
    "pending_detail": 0,
    "detail_errors": 0
  },
  "resume": {
    "provided": true,
    "chars": 133
  },
  "guardrails": {
    "plan_then_confirm": "required upstream of any real submission \u2014 'applypilot apply --plan' / '--i-confirm <hash>' stages, pins, and consumes the batch token; these verbs neither stage nor consume it",
    "browser_work": "the driving agent performs the real submission with its own Chrome/Playwright tooling \u2014 no verb launches an engine or opens a browser",
    "submission": "an apply-result 'applied' records YOUR completed submission \u2014 the verb never submits anything itself"
  }
}
```

- PASS: acquire carries cap headroom for https://jobs.example/epsilon — {"cap": 5, "applied_today": 1, "headroom": 4}

- [simulated browser work: the driving agent submits] → recording applied for `https://jobs.example/epsilon`

```bash
$ applypilot agent apply-result --url https://jobs.example/epsilon --status applied
```

```json
{"stage":"apply","recorded":{"url":"https://jobs.example/epsilon","status":"applied","outcome":"recorded","reason":null,"terminal":false},"apply":{"cap":5,"applied_today":2,"headroom":3},"next_eligible":{"url":"https://jobs.example/gamma","title":"Data Engineer","fit_score":9},"progress":{"total":5,"with_description":5,"scored":5,"unscored":0,"score_distribution":[[9,4],[8,1]],"tailored":5,"untailored_eligible":0,"tailor_exhausted":0,"with_cover_letter":5,"cover_exhausted":0,"pending_detail":0,"detail_errors":0},"guardrails":{"plan_then_confirm":"required upstream of any real submission \u2014 'applypilot apply --plan' / '--i-confirm <hash>' stages, pins, and consumes the batch token; these verbs neither stage nor consume it","browser_work":"the driving agent performs the real submission with its own Chrome/Playwright tooling \u2014 no verb launches an engine or opens a browser","submission":"an apply-result 'applied' records YOUR completed submission \u2014 the verb never submits anything itself"}}
```

```bash
$ applypilot agent apply-acquire
```

```json
{
  "stage": "apply",
  "job": {
    "url": "https://jobs.example/gamma",
    "title": "Data Engineer",
    "site": "exampleboard",
    "location": null,
    "salary": null,
    "fit_score": 9,
    "description_excerpt": "Full posting for Data Engineer: Python, SQL, Airflow, dbt.",
    "description_chars": 58,
    "excerpt_truncated": false
  },
  "application_url": "https://jobs.example/gamma",
  "resume_path": "/home/user/work/dogfood-pipeline/applypilot/tailored_resumes/exampleboard_Data_Engineer_d6a22c79_v1.txt",
  "cover_letter_path": "/home/user/work/dogfood-pipeline/applypilot/cover_letters/exampleboard_Data_Engineer_d6a22c79_v1_CL.txt",
  "acquired_by": "host-worker-0",
  "apply": {
    "cap": 5,
    "applied_today": 2,
    "headroom": 3
  },
  "progress": {
    "total": 5,
    "with_description": 5,
    "scored": 5,
    "unscored": 0,
    "score_distribution": [
      [
        9,
        4
      ],
      [
        8,
        1
      ]
    ],
    "tailored": 5,
    "untailored_eligible": 0,
    "tailor_exhausted": 0,
    "with_cover_letter": 5,
    "cover_exhausted": 0,
    "pending_detail": 0,
    "detail_errors": 0
  },
  "resume": {
    "provided": true,
    "chars": 133
  },
  "guardrails": {
    "plan_then_confirm": "required upstream of any real submission \u2014 'applypilot apply --plan' / '--i-confirm <hash>' stages, pins, and consumes the batch token; these verbs neither stage nor consume it",
    "browser_work": "the driving agent performs the real submission with its own Chrome/Playwright tooling \u2014 no verb launches an engine or opens a browser",
    "submission": "an apply-result 'applied' records YOUR completed submission \u2014 the verb never submits anything itself"
  }
}
```

- PASS: acquire carries cap headroom for https://jobs.example/gamma — {"cap": 5, "applied_today": 2, "headroom": 3}

- [simulated browser work: the driving agent submits] → recording captcha for `https://jobs.example/gamma`

```bash
$ applypilot agent apply-result --url https://jobs.example/gamma --status captcha
```

```json
{"stage":"apply","recorded":{"url":"https://jobs.example/gamma","status":"captcha","outcome":"recorded","reason":null,"terminal":true},"apply":{"cap":5,"applied_today":2,"headroom":3},"next_eligible":{"url":"https://jobs.example/delta","title":"Analytics Engineer","fit_score":8},"progress":{"total":5,"with_description":5,"scored":5,"unscored":0,"score_distribution":[[9,4],[8,1]],"tailored":5,"untailored_eligible":0,"tailor_exhausted":0,"with_cover_letter":5,"cover_exhausted":0,"pending_detail":0,"detail_errors":0},"guardrails":{"plan_then_confirm":"required upstream of any real submission \u2014 'applypilot apply --plan' / '--i-confirm <hash>' stages, pins, and consumes the batch token; these verbs neither stage nor consume it","browser_work":"the driving agent performs the real submission with its own Chrome/Playwright tooling \u2014 no verb launches an engine or opens a browser","submission":"an apply-result 'applied' records YOUR completed submission \u2014 the verb never submits anything itself"}}
```

```bash
$ applypilot agent apply-acquire
```

```json
{
  "stage": "apply",
  "job": {
    "url": "https://jobs.example/delta",
    "title": "Analytics Engineer",
    "site": "exampleboard",
    "location": null,
    "salary": null,
    "fit_score": 8,
    "description_excerpt": "Full posting for Analytics Engineer: Python, SQL, Airflow, dbt.",
    "description_chars": 63,
    "excerpt_truncated": false
  },
  "application_url": "https://jobs.example/delta",
  "resume_path": "/home/user/work/dogfood-pipeline/applypilot/tailored_resumes/exampleboard_Analytics_Engineer_252f703c_v1.txt",
  "cover_letter_path": "/home/user/work/dogfood-pipeline/applypilot/cover_letters/exampleboard_Analytics_Engineer_252f703c_v1_CL.txt",
  "acquired_by": "host-worker-0",
  "apply": {
    "cap": 5,
    "applied_today": 2,
    "headroom": 3
  },
  "progress": {
    "total": 5,
    "with_description": 5,
    "scored": 5,
    "unscored": 0,
    "score_distribution": [
      [
        9,
        4
      ],
      [
        8,
        1
      ]
    ],
    "tailored": 5,
    "untailored_eligible": 0,
    "tailor_exhausted": 0,
    "with_cover_letter": 5,
    "cover_exhausted": 0,
    "pending_detail": 0,
    "detail_errors": 0
  },
  "resume": {
    "provided": true,
    "chars": 133
  },
  "guardrails": {
    "plan_then_confirm": "required upstream of any real submission \u2014 'applypilot apply --plan' / '--i-confirm <hash>' stages, pins, and consumes the batch token; these verbs neither stage nor consume it",
    "browser_work": "the driving agent performs the real submission with its own Chrome/Playwright tooling \u2014 no verb launches an engine or opens a browser",
    "submission": "an apply-result 'applied' records YOUR completed submission \u2014 the verb never submits anything itself"
  }
}
```

- PASS: acquire carries cap headroom for https://jobs.example/delta — {"cap": 5, "applied_today": 2, "headroom": 3}

- [simulated browser work: the driving agent submits] → recording expired for `https://jobs.example/delta`

```bash
$ applypilot agent apply-result --url https://jobs.example/delta --status expired
```

```json
{"stage":"apply","recorded":{"url":"https://jobs.example/delta","status":"expired","outcome":"recorded","reason":null,"terminal":true},"apply":{"cap":5,"applied_today":2,"headroom":3},"next_eligible":null,"progress":{"total":5,"with_description":5,"scored":5,"unscored":0,"score_distribution":[[9,4],[8,1]],"tailored":5,"untailored_eligible":0,"tailor_exhausted":0,"with_cover_letter":5,"cover_exhausted":0,"pending_detail":0,"detail_errors":0},"guardrails":{"plan_then_confirm":"required upstream of any real submission \u2014 'applypilot apply --plan' / '--i-confirm <hash>' stages, pins, and consumes the batch token; these verbs neither stage nor consume it","browser_work":"the driving agent performs the real submission with its own Chrome/Playwright tooling \u2014 no verb launches an engine or opens a browser","submission":"an apply-result 'applied' records YOUR completed submission \u2014 the verb never submits anything itself"}}
```

```bash
$ applypilot agent apply-acquire
```

```
(exit 1)
{
  "error": {
    "code": "no_eligible_job",
    "message": "no job passes the apply queue's filters (tailored, scored >= 7, unlocked, under the attempt limit) \u2014 run the discover/enrich/score/tailor stages first",
    "details": {
      "min_score": 7,
      "apply": {
        "cap": 5,
        "applied_today": 2,
        "headroom": 3
      }
    }
  }
}
```

- PASS: loop ends on a refusal, not a fabricated job — got 'no_eligible_job'

- PASS: three acquired jobs recorded through the host path — recorded 3

```bash
$ applypilot status --json
```

```json
{
  "total": 5,
  "by_site": [
    [
      "exampleboard",
      5
    ]
  ],
  "pending_detail": 0,
  "with_description": 5,
  "detail_errors": 0,
  "scored": 5,
  "unscored": 0,
  "score_distribution": [
    [
      9,
      4
    ],
    [
      8,
      1
    ]
  ],
  "score_provenance": {
    "host": 3,
    "provider": 2,
    "unattributed": 0
  },
  "tailored": 5,
  "untailored_eligible": 0,
  "tailor_exhausted": 0,
  "with_cover_letter": 5,
  "cover_exhausted": 0,
  "applied": 2,
  "captcha": 2,
  "apply_errors": 1,
  "ready_to_apply": 3
}
```

- PASS: status --json: one more application recorded today — applied=2

- PASS: status --json: captcha separate; expired counts as the one apply error — captcha=2, apply_errors=1

- PASS: journey outcomes cover the vocabulary: applied, captcha, expired — {"https://jobs.example/gamma": "captcha", "https://jobs.example/delta": "expired", "https://jobs.example/epsilon": "applied"}

- PASS: only the applied journey job has applied_at

- PASS: journey made zero provider calls — 8 → 8

- PASS: journey made zero claude engine calls — 34 → 34


## 8 · Provider call ledger

- provider calls by kind: `{"score": 2, "tailor": 2, "judge": 2, "cover_letter": 2}`

- fake-claude turns logged: `/home/user/work/dogfood-pipeline/claude-prompts.log`


## Verdict

All checks passed — every protocol verb ran end-to-end; every error

in this transcript is an intentional, labeled probe of the error

envelope: the onboarding unknown-question refusal, the burned-token

replay refusal, the score verb's invalid_score / already_scored

refusals, the tailor validator's retryable invalid_resume_data

envelope, and the apply loop's not_acquired / no_eligible_job

refusals. The host-driven pipeline journey (section 7) ran the

whole pipeline — score, enrich, tailor, cover, apply bookkeeping

— with zero provider and zero claude calls, proven by the ledger

deltas in its closing checks.
