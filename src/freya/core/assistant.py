import json

from freya.config import SYSTEM_PROMPT, CONTEXT_WINDOW_TOKENS, CONTEXT_OUTPUT_TOKENS
from freya.kernel.context import ContextAuthority, ContextBudget, ContextItem, estimated_tokens
from freya.core.conversation_memory import (
    NOT_CONSULTED,
    NOTHING_FOUND,
    SEARCHED,
    ConversationMemory,
)
from freya.core.llm import (
    chat_stream,
    evaluate_memory,
    rerank_memory_candidates,
)
from freya.core.memory_commands import MemoryCommands
from freya.memory.memory_manager import MemoryManager
from freya.memory.deletion_ledger import StorageQuarantined


MAX_TOOL_ROUNDS = 3
MAX_TOOL_CALLS = 12

# Said when the model returns no words at all.
EMPTY_REPLY_NOTICE = (
    "I did not manage to put an answer together for that. I may have "
    "spent all the room I am allowed on working it out. Ask me again, "
    "or for a smaller part of it."
)


class FreyaAssistant(MemoryCommands, ConversationMemory):
    def __init__(self, tool_host=None, broker=None, memory_ledger_path=None):
        # With a ledger path, the memory database is opened in
        # protected mode (see MemoryManager). Off unless asked for.
        self.memory = MemoryManager(memory_ledger_path)

        # Optional: offers tools to the model and runs the ones it
        # asks for. Without it Freya answers from the model alone.
        self.tool_host = tool_host
        # Optional: lets the model hand one piece of work to an outside
        # model during a turn (see freya.delegation.turn). Without it a
        # turn is exactly what it always was.
        self.delegation = None
        # What she reads from her saved memories and conversations for
        # a reply is asked for through the broker. Without one, nothing
        # stored is given to the model.
        self.broker = broker
        # Counting every byte as a token left room for only a few
        # exchanges of the live conversation; this estimate still errs high.
        self.context = ContextAuthority(
            ContextBudget(CONTEXT_WINDOW_TOKENS, CONTEXT_OUTPUT_TOKENS),
            counter=estimated_tokens,
        )

        self.messages = [
            {
                "role": "system",
                "content": SYSTEM_PROMPT,
            }
        ]

        self._start_conversations()

    def respond_stream(self, user_input):
        # The inputs a delegation could send are fixed here, before
        # the model is first called, and dropped when the turn ends,
        # whether it ends well, in an error, or by being closed.
        turn = self._begin_delegation(user_input)

        try:
            yield from self._respond_stream(user_input, turn)
        finally:
            self._end_delegation(turn)

    def _begin_delegation(self, user_input):
        if self.delegation is None:
            return None

        try:
            return self.delegation.begin_turn(
                user_input, self.messages, self.source,
            )
        except Exception:
            # Handing work over is optional; a fault in it must not
            # stop a reply.
            return None

    def _end_delegation(self, turn):
        if self.delegation is not None:
            try:
                self.delegation.end_turn(turn)
            except Exception:
                pass

    def _respond_stream(self, user_input, turn):
        command_response = (
            self._handle_memory_command(
                user_input
            )
        )

        if command_response is not None:
            yield command_response
            return

        tools = self.tool_host.definitions() if self.tool_host is not None else []
        messages_for_model = self._prepare_context(user_input, tools)

        reply_parts = []
        tool_attempts = 0

        for round_number in range(MAX_TOOL_ROUNDS + 1):
            # After the last allowed round the model must answer
            # with what it has.
            offered = (
                tools
                if round_number < MAX_TOOL_ROUNDS
                and tool_attempts < MAX_TOOL_CALLS
                else None
            )

            messages_for_model = self.context.fit_tools(messages_for_model, offered)
            tool_calls = []

            for chunk in chat_stream(
                messages_for_model,
                tools=offered,
                model_options=self.context.options(),
            ):
                if isinstance(chunk, dict):
                    tool_calls.extend(
                        chunk.get("tool_calls", [])
                    )
                    continue

                reply_parts.append(chunk)
                yield chunk

            if not tool_calls:
                break

            # The tool exchange is only needed for this turn; it
            # is not kept in the conversation history.
            messages_for_model.append(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "function": {
                                "name": call["name"],
                                "arguments": call["arguments"],
                            }
                        }
                        for call in tool_calls
                    ],
                }
            )

            for call in tool_calls:
                # Removing definitions is not enforcement: a model
                # may still request a tool on the final round.
                if not offered or tool_attempts >= MAX_TOOL_CALLS:
                    result = json.dumps({"error": (
                        "The tool budget is exhausted. This request was not run."
                    )})
                else:
                    tool_attempts += 1
                    result = self.tool_host.run(call["name"], call["arguments"])
                    if turn is not None:
                        turn.add_tool_result(call["name"], result)
                messages_for_model.append(
                    {
                        "role": "tool",
                        "tool_name": call["name"],
                        "content": result,
                    }
                )

            if round_number == MAX_TOOL_ROUNDS:
                notice = (
                    "\n\nI reached the tool limit. "
                    "Some requested checks were not completed."
                )
                reply_parts.append(notice)
                yield notice

        reply = "".join(
            reply_parts
        ).strip()

        if not reply:
            # The model can spend its whole allowance working a
            # problem out and have nothing left to answer with.
            # Say so: silence would look like a fault, and the
            # question would vanish from the conversation.
            reply = EMPTY_REPLY_NOTICE
            yield reply

        self.messages.append(
            {
                "role": "user",
                "content": user_input,
            }
        )

        self.messages.append(
            {
                "role": "assistant",
                "content": reply,
            }
        )

        self._keep_exchange(
            user_input,
            reply,
        )

        self._process_automatic_memory(
            user_input
        )

    def _prepare_context(self, user_input, tools=None):
        """One context path for typed, streamed, and tool-assisted replies."""
        if self.memory.ledger is not None and self.memory.ledger.quarantined:
            raise StorageQuarantined("Reconcile quarantined memory storage before answering.")
        items, retrieval_notes = [], []
        if self._live_summary:
            # Her summary of the older part of this conversation stands
            # in for live dialogue; it is not a read of what is stored.
            items.append(ContextItem(
                "conversation-summary", f"conversation:{self.conversations.current}",
                self._live_summary, "model-inference", 80,
            ))
        # One authorized, audited read of what is stored. Nothing it
        # returns is kept beyond this reply.
        stored = self._recall_for_reply(user_input)
        if stored is None:
            retrieval_notes.append(NOT_CONSULTED)
        else:
            retrieval_notes += stored["notes"]
            items += self._stored_items(user_input, stored, retrieval_notes)
        callback = getattr(self, "media_recall", None)
        if callback is not None:
            try:
                result = callback(user_input[:200])
                if result is None:
                    # Media recall is not enabled: nothing was asked for,
                    # so there is nothing to tell the model.
                    pass
                elif result.success:
                    for record in result.result.get("results", [])[:3]:
                        items.append(ContextItem(
                            "media", f"media:{record['media_id']}:{record.get('artifact_id') or record.get('at_seconds')}",
                            json.dumps(record, ensure_ascii=False), record.get("trust") or "untrusted-source", 70,
                            owner=record.get("owner") or "local-owner",
                            domain=record.get("domain") or "local",
                            permitted_uses=tuple(record.get("permitted_uses", ("answer",))),
                        ))
                else:
                    retrieval_notes.append("Media retrieval was unavailable or not authorized.")
            except Exception:
                retrieval_notes.append("Media retrieval was unavailable.")
        if retrieval_notes:
            items.append(ContextItem("retrieval-status", "retrieval-status", " ".join(retrieval_notes), "system-observation", 95))
        result = self.context.build(SYSTEM_PROMPT, self.messages, user_input, items,
                                    tools=tools, tool_hint=self.tool_host.hint if tools and self.tool_host else "")
        self.context.last_report["retrieval_notes"] = retrieval_notes
        return result

    @staticmethod
    def _memory_item(record):
        return ContextItem(
            "saved-memory", f"memory:{record['id']}",
            f"Type: {record['type']}; key: {record.get('key') or 'unkeyed'}; {record['content']}",
            record.get("source_trust") or "legacy-unverified", 90,
            owner=record.get("owner") or "local-owner",
            domain=record.get("domain") or "local",
            permitted_uses=tuple(record.get("permitted_uses", ("answer",))),
        )

    def _stored_items(self, user_input, stored, retrieval_notes):
        """What was read from memory, as labelled records for this reply."""
        items = [
            ContextItem(
                "conversation-summary", f"conversation:{summary['conversation']}",
                f"{summary['date']}: {summary['summary']}", "model-inference", 50,
            )
            for summary in stored["summaries"]
        ]
        # Authorization precedes relevance ranking: an LLM reranker must
        # never see a candidate outside the active context scope.
        memories = [
            record for record in stored["memories"]
            if self.context.permits(self._memory_item(record))
        ]
        if memories and not stored["asked_about_past"]:
            try:
                ranking = rerank_memory_candidates(query=user_input, candidates=memories, max_results=3)
                selected_ids = set(ranking.get("relevant_ids", []))
                memories = [record for record in memories if record["id"] in selected_ids][:3]
            except Exception:
                memories = memories[:3]
                retrieval_notes.append("Memory relevance ranking was unavailable; candidate order was used.")
        items += [self._memory_item(record) for record in memories]
        for passage in stored["passages"]:
            items.append(ContextItem(
                "conversation-passage",
                f"conversation:{passage['conversation']}:turns:{','.join(map(str, passage['turn_ids']))}",
                "\n".join(
                    [f"Conversation of {passage['date']}:"]
                    + [f"{entry['who']}: {entry['said']}" for entry in passage["exchange"]]
                ),
                "quoted-conversation", 70,
            ))
        found = memories or stored["passages"]
        # A search that partly failed is not told to her as "nothing
        # was found"; the retrieval notes say what could not be searched.
        if stored["asked_about_past"] and (found or not stored["notes"]):
            note = SEARCHED if found else NOTHING_FOUND
            items.append(ContextItem(
                "past-recall", "past-recall",
                note.format(words=", ".join(stored["searched"])), "system-observation", 100,
            ))
        return items

    def _process_automatic_memory(
        self,
        user_input,
    ):
        candidate = evaluate_memory(
            user_input
        )

        if candidate is None:
            return

        self.memory.process_candidate(
            candidate
        )
