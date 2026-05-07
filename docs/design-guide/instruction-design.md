---
title: Instruction Design
description: How to write agent instructions that produce consistent, reliable behavior.
---

# Instruction Design

Agent instructions are not documentation — they are executable specifications. The model reads them at runtime and uses them to make decisions. Vague instructions produce inconsistent behavior; precise, structured instructions produce reliable behavior.

The single most effective practice is to use XML tags to give the model unambiguous structure. When instructions are a wall of prose, the model has to infer boundaries between concepts. XML tags make those boundaries explicit.

---

## XML tag reference

These tags form the standard vocabulary for CXAS agent instructions:

| Tag | Purpose |
|-----|---------|
| `<role>` | Defines who this agent is — unique to this agent, not shared with others |
| `<persona>` | Sets tone and communication style — can be shared globally across agents |
| `<primary_goal>` | One sentence: what this agent exists to accomplish |
| `<constraints>` | Hard limits on what the agent can and cannot do |
| `<taskflow>` | The ordered sequence of steps the agent follows in a conversation |
| `<subtask>` | A named unit of work within the taskflow |
| `<step>` | A single action or decision within a subtask |
| `<trigger>` | A condition that causes a step or branch to activate |
| `<action>` | The specific thing the agent does when a trigger fires |
| `<examples>` | Few-shot demonstrations of correct behavior |

Not every instruction needs every tag. Use the tags that match the complexity of the behavior you're specifying.

---

## A complete instruction example

The following is a representative instruction set for a restaurant reservation agent (Bella Notte). It demonstrates how the tags compose in practice.

```xml
<role>
You are the reservations assistant for Bella Notte, an upscale Italian restaurant
in San Francisco. You help guests make, modify, and cancel reservations. You have
no knowledge of menu items, pricing, or kitchen operations — guests with those
questions should be directed to the restaurant directly.
</role>

<persona>
Warm and professional. Use the guest's name when you know it. Be concise — guests
are often on mobile. Never use filler phrases like "Certainly!" or "Of course!".
</persona>

<primary_goal>
Complete reservation requests accurately and confirm all details before finalizing.
</primary_goal>

<constraints>
- Never confirm a reservation without collecting: date, time, party size, and guest name.
- Never book a party larger than 12 — direct larger parties to events@bellanotte.com.
- Never modify a reservation made more than 24 hours in the future without
  calling the check_modification_policy tool first.
- If the requested time is unavailable, always offer exactly two alternatives.
</constraints>

<taskflow>
  <subtask name="gather_reservation_details">
    <step>Ask for the reservation date if not provided.</step>
    <step>Ask for the party size if not provided.</step>
    <step>Ask for the preferred time if not provided.</step>
    <step>Ask for the guest name if not provided.</step>
    <step>Once all four are collected, call check_availability.</step>
  </subtask>

  <subtask name="handle_availability_response">
    <trigger>check_availability returns available=true</trigger>
    <action>Present the slot to the guest and ask for confirmation.</action>

    <trigger>check_availability returns available=false</trigger>
    <action>Call get_alternative_slots and present exactly two options to the guest.</action>
  </subtask>

  <subtask name="confirm_reservation">
    <trigger>Guest confirms a slot</trigger>
    <action>Call create_reservation with all collected details.</action>
    <action>Read back the confirmation number and full details to the guest.</action>
  </subtask>
</taskflow>

<examples>
Guest: "I'd like a table for Saturday."
Assistant: "Happy to help. How many people will be joining you, and do you have a
preferred time?"

Guest: "Table for 4 at 7pm, but I'm flexible on the day."
Assistant: "What date were you thinking — this weekend, or further out?"
</examples>
```

---

## Good vs. bad instruction patterns

### Role definition

=== "Bad"

    ```xml
    <role>
    You are a helpful assistant that helps with reservations.
    </role>
    ```

    This says nothing specific. "Helpful assistant" describes every chatbot. The model has no idea what it can't do, which restaurant this is, or how to handle out-of-scope questions.

=== "Good"

    ```xml
    <role>
    You are the reservations assistant for Bella Notte, an upscale Italian restaurant
    in San Francisco. You handle reservation creation, modification, and cancellation.
    Questions about the menu, pricing, or events are outside your scope — direct
    those guests to the restaurant directly at info@bellanotte.com.
    </role>
    ```

    The role is specific to this agent. It defines what's in scope and what's out, and it gives the model an explicit redirect for out-of-scope requests.

### Constraints

=== "Bad"

    ```xml
    <constraints>
    Be polite and don't do anything inappropriate.
    </constraints>
    ```

    "Inappropriate" is undefined. The model will interpret this however it wants, which means this constraint provides no real behavioral guarantee.

=== "Good"

    ```xml
    <constraints>
    - Never confirm a reservation without all four required fields: date, time,
      party size, and guest name.
    - Never accept a party size greater than 12. If the guest requests more than 12,
      say: "For parties larger than 12, please contact our events team at
      events@bellanotte.com."
    - If the requested time slot is unavailable, always offer exactly two alternatives.
      Never offer one or three.
    </constraints>
    ```

    Each constraint is a testable behavioral rule. You can write an eval that verifies it.

---

## The Actionability Test

When writing a step or constraint, apply this test: **can you write a failing eval for it?**

If the answer is no — if you can't describe what "wrong" looks like in concrete terms — the instruction is too vague to be reliable.

For example:

- "Be helpful" — fails the test. You can't write an eval for "helpful."
- "Always call `check_availability` before calling `create_reservation`" — passes the test. You can write an eval that checks tool call order.
- "Offer alternatives if the requested time is unavailable" — passes the test. You can write an eval that mocks an unavailability response and checks that the agent calls `get_alternative_slots`.

Run the Actionability Test on every instruction you write. If it fails, rewrite the instruction until it passes.

---

## Key rules

**Persona is global, role is unique.** If you're running multiple agents in a multi-agent setup, the `<persona>` tag defines shared communication style and can be the same across all agents. The `<role>` tag must be unique to each agent — it defines what that specific agent does and doesn't do.

**Examples are few-shot demonstrations, not documentation.** The `<examples>` block teaches the model by showing it correct input/output pairs. Keep examples short (2–4 exchanges) and focused on the patterns you most want to reinforce — typically the patterns that are most likely to go wrong without guidance.

**Order matters in taskflows.** The model treats steps in a `<subtask>` as an ordered sequence. If you want the model to always ask for date before party size, put date first. If the order doesn't matter, say so explicitly: "Collect the following in any order."

**Don't bury constraints in prose.** If a constraint is important, put it in the `<constraints>` block. Constraints buried inside `<taskflow>` prose are more likely to be overlooked by the model.

!!! warning "Instruction length and reliability"
    Every token in your instructions is a token the model must attend to. As instructions grow, the model's ability to follow all of them simultaneously decreases. If your instructions exceed roughly 2,000 tokens, consider splitting functionality across multiple agents or using dynamic prompting via callbacks to inject only the relevant context for a given turn. See [Callbacks](callbacks.md) for dynamic prompting patterns.
