# Foodie AI — User stories and text-message storyboards

All stories are target behaviors, not existing deployed features. [spec.md](spec.md) has the normative acceptance tests. In the examples, restaurant listings must be populated with real provider data before live use.

## US-01: Joining and onboarding
**As** a group organizer, **I want** the assistant to introduce itself once and ask for agreement before participation.

```text
Alex: Foodie AI setup
Foodie AI: I'm a food-planning assistant. When called, I process relevant new messages and may use a hosted Jev service if enabled. Is everyone comfortable with that?
Group: [explicit consent]
Foodie AI: What should this group call me? We can vote, or stick with Foodie.
Group: [vote for Chef]
Chef: Got it. What's the usual city for dining? I won't save it until confirmed.
```
**Acceptance:** allowlisted group, authorized setup, once-only intro, consent recorded, no surprise reading of earlier messages.

## US-02: On-demand activation
```text
Alex: Anybody hungry?
Sam: Yeah
[No agent reply or AI call]
Alex: Chef help us pick dinner
Chef: What area should I search? Use the group's saved city?
```
**Acceptance:** unrelated dormant traffic discarded; wake creates one session.

## US-03: Live place search
```text
Alex: Chef find Mexican food under $25 near Tempe
Chef: Searching near Tempe within the saved five-mile radius.
Chef: 1. <real place> — <real available rating and review count> — Map: <URL>
      2. <real place> — ...
      Hours: unverified where unavailable. Want to vote?
```
**Acceptance:** true provider data, zero fabricated attributes, valid map URLs, no false real-time availability assertions.

## US-04: Competing cuisines and budget
```text
Alex: Chef sushi please
Mike: Burgers for me
Sam: $15 is a hard maximum
Chef: I'll keep the $15 cap and look for a place with both options. If none fits, I'll show a choice from each cuisine for a vote.
```
**Acceptance:** store each member's preference; strict attendee budget remains enforced.

## US-05: Mid-search correction
```text
Alex: Chef find burgers in Mesa
Mike: Actually chicken instead
Sam: Five miles max
Chef: Switching to chicken within five miles of Mesa.
```
**Acceptance:** no burger shortlist produced by old version after the correction; one concise response.

## US-06: Allergy and halal requirements
```text
Mike: Chef, peanut allergy. Treat it as a requirement.
Sam: Halal for me.
Chef: I'll flag places without evidence rather than assume they're safe. Please confirm allergens and food preparation directly with a restaurant before visiting.
```
**Acceptance:** not silently relaxed by majority vote or an AI output; unverified candidates never labeled allergy-safe or halal-certified.

## US-07: Vote and finalize
```text
Alex: Chef vote
Chef: Vote on the current shortlist. Say "Chef vote 1", "Chef vote 2", or "Chef vote 3".
Mike: Chef vote 2
Sam: Chef vote 1
Mike: Chef vote 1
Chef: The current tally is 2 for option 1.
```
**Acceptance:** Mike's changed vote replaces old vote; votes reference stable shortlist version; ties resolved with one tiebreaker.

## US-08: Someone joins late
```text
Jordan: I'm joining, need vegetarian options.
Chef: I can check whether the chosen place has verified vegetarian options. Want to reopen the search if needed?
```
**Acceptance:** do not silently overturn a finalized plan; include late participant only after confirming participation.

## US-09: User deletes saved preferences
```text
Sam: Chef forget me
Chef: I've deleted your saved preferences for this group.
```
**Acceptance:** only Sam's durable preferences are removed; do not claim deletion unless DB transaction succeeded.

## US-10: Technical interruption
```text
Alex: Chef find food
[Mac sleeps; app unavailable]
[Mac wakes 20 minutes later]
Alex: Chef are you back?
Chef: I'm back. The earlier search expired. Want to start fresh?
```
**Acceptance:** no stale results or duplicate replies replayed, no fabricated knowledge of messages missed while offline.

## US-11: Personality change
```text
Alex: Chef you're too formal
Chef: Want to vote on a different style?
Mike: Funny, no emoji spam
Sam: Just straightforward
Chef: I'll put both options in a short poll.
```
**Acceptance:** group-controlled poll, ties maintain current personality; change doesn't affect other groups or policy.

## US-12: Stop immediately
```text
Alex: Chef find burgers
Sam: Chef stop
Chef: Stopped. Call me again when you're ready.
```
**Acceptance:** no delayed restaurant replies after stop and no Jev calls for unrelated dormant chat messages.
