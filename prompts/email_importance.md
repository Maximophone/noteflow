You are evaluating the importance of emails to determine which should be included in a daily digest.

For each email, score its importance from 1-10 based on these criteria:

**High importance (7-10):**
- Personal emails from real individuals (colleagues, clients, friends, family)
- Actionable content (requests, questions, decisions needed)
- Professional correspondence about ongoing projects
- Direct replies to your emails

**Medium importance (4-6):**
- Semi-automated but relevant (order confirmations, appointment reminders)
- Newsletters you actively read and find valuable
- Professional community discussions you participate in

**Low importance (1-3):**
- Marketing and promotional emails
- Mass newsletters and subscriptions
- Automated notifications that don't require action
- Social media notifications
- Spam or unsolicited content

Return a JSON array with your evaluation for each email:

```json
[
  {{"email_id": "...", "score": 8, "reason": "Direct email from colleague about project deadline"}},
  {{"email_id": "...", "score": 2, "reason": "Marketing newsletter from subscription"}}
]
```

Here are the emails to evaluate:

{emails}
