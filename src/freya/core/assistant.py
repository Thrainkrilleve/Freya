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
    classify_legacy_memory,
    compare_memories,
    evaluate_memory,
    rerank_memory_candidates,
)
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


class FreyaAssistant(ConversationMemory):
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

    def _memory_search_diagnostic(
        self,
        query,
    ):
        results = self.memory.search_memories(
            query
        )

        if not results:
            return (
                "Memory search results for:\n"
                f'"{query}"\n\n'
                "No memories met the current "
                "similarity threshold.\n\n"
                "READ-ONLY DIAGNOSTIC - "
                "no memories have been changed."
            )

        lines = [
            "Memory search results for:",
            f'"{query}"',
            "",
        ]

        for memory in results:
            lines.append(
                f"#{memory['id']} "
                f"[{memory['type']}] "
                f"[{memory['key'] or 'unkeyed'}]"
            )
            lines.append(
                "Similarity: "
                f"{memory['similarity']:.4f}"
            )
            lines.append(
                "Content: "
                f"{memory['content']}"
            )
            lines.append("")

        lines.append(
            "READ-ONLY DIAGNOSTIC - "
            "no memories have been changed."
        )

        return "\n".join(lines)

    def _memory_llm_rerank_diagnostic(
        self,
        query,
    ):
        candidates = (
            self.memory.search_memory_candidates(
                query,
                limit=5,
            )
        )

        if not candidates:
            return (
                "LLM memory rerank results for:\n"
                f'"{query}"\n\n'
                "No embedding candidates met the "
                "current similarity threshold.\n\n"
                "READ-ONLY DIAGNOSTIC - "
                "normal retrieval has not changed."
            )

        result = rerank_memory_candidates(
            query=query,
            candidates=candidates,
            max_results=3,
        )

        relevant_ids = set(
            result["relevant_ids"]
        )

        lines = [
            "LLM memory rerank results for:",
            f'"{query}"',
            "",
        ]

        for memory in candidates:
            decision = (
                "KEEP"
                if memory["id"] in relevant_ids
                else "REJECT"
            )

            lines.append(
                f"#{memory['id']} "
                f"[{memory['type']}] "
                f"[{memory['key'] or 'unkeyed'}]"
            )
            lines.append(
                "Similarity: "
                f"{memory['similarity']:.4f}"
            )
            lines.append(
                f"Decision: {decision}"
            )
            lines.append(
                "Content: "
                f"{memory['content']}"
            )
            lines.append("")

        lines.append(
            "Reranker reason: "
            f"{result['reason']}"
        )
        lines.append("")
        lines.append(
            "READ-ONLY DIAGNOSTIC - "
            "normal conversation retrieval has "
            "NOT been changed."
        )

        return "\n".join(lines)

    def _memory_rerank_diagnostic(
        self,
        query,
    ):
        candidates = self.memory.search_memory_candidates(
            query,
            limit=5,
        )

        if not candidates:
            return (
                "Memory rerank results for:\n"
                f'"{query}"\n\n'
                "No embedding candidates met the "
                "current similarity threshold.\n\n"
                "READ-ONLY DIAGNOSTIC - "
                "normal retrieval has not changed."
            )

        personal_terms = {
            "my", "mine", "i", "me",
            "we", "our", "ours",
        }

        def words(text):
            cleaned = "".join(
                char.lower() if char.isalnum() else " "
                for char in text
            )
            return set(cleaned.split())

        query_words = words(query)
        asks_personal = bool(query_words & personal_terms)

        lines = [
            "Memory rerank results for:",
            f'"{query}"',
            "",
            "Query intent: "
            + ("PERSONAL" if asks_personal else "GENERAL/UNCERTAIN"),
            "",
        ]

        for memory in candidates:
            key_words = words(
                (memory["key"] or "").replace("_", " ")
            )
            content_words = words(memory["content"])
            overlap = query_words & (key_words | content_words)

            if not asks_personal:
                decision = "REJECT"
                reason = (
                    "No clear personal-memory signal "
                    "in the query."
                )
            elif overlap:
                decision = "KEEP"
                reason = (
                    "Personal query with direct "
                    "key/content overlap: "
                    + ", ".join(sorted(overlap))
                )
            else:
                decision = "REVIEW"
                reason = (
                    "Semantically similar, but no "
                    "direct key/content overlap."
                )

            lines.append(
                f"#{memory['id']} "
                f"[{memory['type']}] "
                f"[{memory['key'] or 'unkeyed'}]"
            )
            lines.append(
                f"Similarity: {memory['similarity']:.4f}"
            )
            lines.append(f"Decision: {decision}")
            lines.append(f"Reason: {reason}")
            lines.append(f"Content: {memory['content']}")
            lines.append("")

        lines.append(
            "READ-ONLY DIAGNOSTIC - "
            "normal conversation retrieval has NOT been changed."
        )
        return "\n".join(lines)

    def _compare_memories(
        self,
        memory_id_a,
        memory_id_b,
    ):
        if memory_id_a == memory_id_b:
            return (
                "Please provide two different "
                "memory IDs."
            )

        memory_a = self.memory.get_memory(
            memory_id_a
        )

        memory_b = self.memory.get_memory(
            memory_id_b
        )

        if memory_a is None:
            return (
                f"I couldn't find memory "
                f"{memory_id_a}."
            )

        if memory_b is None:
            return (
                f"I couldn't find memory "
                f"{memory_id_b}."
            )

        result = compare_memories(
            memory_a,
            memory_b,
        )

        suggested_key = (
            result["suggested_key"]
            if result["suggested_key"]
            else "none"
        )

        return (
            f"Memory comparison: "
            f"#{memory_id_a} vs "
            f"#{memory_id_b}\n\n"
            f"Memory #{memory_id_a}:\n"
            f"[{memory_a['type']}] "
            f"[{memory_a['key'] or 'unkeyed'}] "
            f"{memory_a['content']}\n\n"
            f"Memory #{memory_id_b}:\n"
            f"[{memory_b['type']}] "
            f"[{memory_b['key'] or 'unkeyed'}] "
            f"{memory_b['content']}\n\n"
            "Assessment: "
            f"{result['relationship'].upper()}\n"
            "Reason: "
            f"{result['reason']}\n"
            "Suggested semantic key: "
            f"{suggested_key}\n\n"
            "READ-ONLY ANALYSIS - "
            "no memories have been changed."
        )

    def _resolve_duplicate(
        self,
        keep_id,
        remove_id,
    ):
        if keep_id == remove_id:
            return (
                "Please provide two different "
                "memory IDs."
            )

        keep_memory = self.memory.get_memory(
            keep_id
        )

        remove_memory = self.memory.get_memory(
            remove_id
        )

        if keep_memory is None:
            return (
                f"I couldn't find memory "
                f"{keep_id}."
            )

        if remove_memory is None:
            return (
                f"I couldn't find memory "
                f"{remove_id}."
            )

        comparison = compare_memories(
            keep_memory,
            remove_memory,
        )

        if (
            comparison["relationship"]
            != "duplicate"
        ):
            return (
                "Duplicate resolution blocked.\n\n"
                f"Freya classified memory "
                f"#{keep_id} and memory "
                f"#{remove_id} as "
                f"{comparison['relationship'].upper()}.\n"
                f"Reason: "
                f"{comparison['reason']}\n\n"
                "No memories have been changed."
            )

        memory_key = comparison[
            "suggested_key"
        ]

        if not memory_key:
            return (
                "Duplicate resolution blocked.\n\n"
                "Freya did not produce a valid "
                "shared semantic key.\n\n"
                "No memories have been changed."
            )

        keep_candidate = (
            classify_legacy_memory(
                keep_memory["content"]
            )
        )

        remove_candidate = (
            classify_legacy_memory(
                remove_memory["content"]
            )
        )

        if (
            keep_candidate is None
            or remove_candidate is None
        ):
            return (
                "Duplicate resolution blocked.\n\n"
                "Freya could not safely classify "
                "both memories.\n\n"
                "No memories have been changed."
            )

        if (
            keep_candidate["key"]
            != memory_key
            or remove_candidate["key"]
            != memory_key
        ):
            return (
                "Duplicate resolution blocked.\n\n"
                "The semantic-key checks did not "
                "agree.\n"
                f"Comparison key: "
                f"{memory_key}\n"
                f"Memory #{keep_id} key: "
                f"{keep_candidate['key']}\n"
                f"Memory #{remove_id} key: "
                f"{remove_candidate['key']}\n\n"
                "No memories have been changed."
            )

        if (
            keep_candidate["type"]
            == remove_candidate["type"]
        ):
            canonical_type = (
                keep_candidate["type"]
            )
        elif (
            keep_candidate["type"]
            == "other"
        ):
            canonical_type = (
                remove_candidate["type"]
            )
        elif (
            remove_candidate["type"]
            == "other"
        ):
            canonical_type = (
                keep_candidate["type"]
            )
        else:
            return (
                "Duplicate resolution blocked.\n\n"
                "The memories received different "
                "non-generic memory types:\n"
                f"Memory #{keep_id}: "
                f"{keep_candidate['type']}\n"
                f"Memory #{remove_id}: "
                f"{remove_candidate['type']}\n\n"
                "No memories have been changed."
            )

        try:
            resolved = (
                self.memory.resolve_duplicate(
                    keep_id=keep_id,
                    remove_id=remove_id,
                    memory_type=canonical_type,
                    memory_key=memory_key,
                )
            )

        except Exception as error:
            return (
                "Duplicate resolution blocked.\n\n"
                f"{error}\n\n"
                "No partial resolution was "
                "committed."
            )

        if not resolved:
            return (
                "Duplicate resolution failed.\n\n"
                "No confirmed database change "
                "was completed."
            )

        surviving_memory = (
            self.memory.get_memory(
                keep_id
            )
        )

        return (
            "Duplicate resolution complete.\n\n"
            f"Kept memory: #{keep_id}\n"
            f"Removed duplicate: "
            f"#{remove_id}\n"
            f"Canonical type: "
            f"{canonical_type}\n"
            f"Canonical key: "
            f"{memory_key}\n"
            f"Content preserved from "
            f"#{keep_id}:\n"
            f"{surviving_memory['content']}\n\n"
            "Both original states were archived "
            "in memory history."
        )

    def _restore_memory(
        self,
        memory_id,
        history_id,
    ):
        current = self.memory.get_memory(
            memory_id
        )

        if current is None:
            return (
                f"I couldn't find live memory "
                f"{memory_id}."
            )

        history = (
            self.memory.get_history_entry(
                history_id
            )
        )

        if history is None:
            return (
                f"I couldn't find history entry "
                f"{history_id}."
            )

        if history["memory_id"] != memory_id:
            return (
                "Memory restore blocked.\n\n"
                f"History #{history_id} belongs "
                f"to memory "
                f"#{history['memory_id']}, not "
                f"memory #{memory_id}.\n\n"
                "No memories have been changed."
            )

        result = self.memory.restore_memory(
            memory_id=memory_id,
            history_id=history_id,
        )

        if not result["success"]:
            reason = result["reason"]

            if reason == "key_collision":
                return (
                    "Memory restore blocked.\n\n"
                    "The historical semantic key "
                    "is already used by live "
                    f"memory "
                    f"#{result['collision_id']}.\n\n"
                    "No memories have been "
                    "changed."
                )

            if reason == "memory_not_found":
                return (
                    "Memory restore blocked "
                    "because the live memory no "
                    "longer exists."
                )

            if reason == "history_not_found":
                return (
                    "Memory restore blocked "
                    "because the history entry no "
                    "longer exists."
                )

            if reason == "history_mismatch":
                return (
                    "Memory restore blocked "
                    "because the history entry "
                    "does not belong to that "
                    "memory."
                )

            if reason == "transaction_failed":
                return (
                    "Memory restore failed and "
                    "was rolled back.\n\n"
                    f"Reason: "
                    f"{result.get('error', 'unknown error')}\n\n"
                    "No partial restore was "
                    "committed."
                )

            return (
                "Memory restore failed safely. "
                "No changes were committed."
            )

        restored = result["memory"]

        return (
            "Memory restore complete.\n\n"
            f"Restored memory: "
            f"#{memory_id}\n"
            f"Source history: "
            f"#{history_id}\n"
            f"Type: "
            f"{restored['type']}\n"
            f"Key: "
            f"{restored['key'] or 'unkeyed'}\n"
            f"Content: "
            f"{restored['content']}\n\n"
            "The previous live state was "
            "archived as a restore checkpoint, "
            "so this restore can be reversed."
        )

    def _build_migration_plan(self):
        memories = (
            self.memory.get_all_memories()
        )

        legacy_memories = []

        for memory in memories:
            (
                memory_id,
                memory_type,
                memory_key,
                content,
                created_at,
                updated_at,
            ) = memory

            if memory_key is None:
                legacy_memories.append(
                    {
                        "id": memory_id,
                        "type": memory_type,
                        "key": memory_key,
                        "content": content,
                    }
                )

        if not legacy_memories:
            self.memory.clear_migration_plan()

            return []

        proposals = []

        for memory in legacy_memories:
            candidate = (
                classify_legacy_memory(
                    memory["content"]
                )
            )

            if candidate is None:
                proposals.append(
                    {
                        "id": memory["id"],
                        "current_type": (
                            memory["type"]
                        ),
                        "current_key": None,
                        "content": (
                            memory["content"]
                        ),
                        "proposed_type": None,
                        "proposed_key": None,
                        "blocked": True,
                        "block_reason": (
                            "Classification failed."
                        ),
                    }
                )

                continue

            proposals.append(
                {
                    "id": memory["id"],
                    "current_type": (
                        memory["type"]
                    ),
                    "current_key": None,
                    "content": (
                        memory["content"]
                    ),
                    "proposed_type": (
                        candidate["type"]
                    ),
                    "proposed_key": (
                        candidate["key"]
                    ),
                    "blocked": False,
                    "block_reason": None,
                }
            )

        key_groups = {}

        for proposal in proposals:
            key = proposal[
                "proposed_key"
            ]

            if not key:
                continue

            key_groups.setdefault(
                key,
                [],
            ).append(
                proposal["id"]
            )

        for proposal in proposals:
            key = proposal[
                "proposed_key"
            ]

            if not key:
                continue

            matching_ids = (
                key_groups.get(
                    key,
                    [],
                )
            )

            if len(matching_ids) > 1:
                other_ids = [
                    memory_id
                    for memory_id
                    in matching_ids
                    if memory_id
                    != proposal["id"]
                ]

                formatted_ids = ", ".join(
                    f"#{memory_id}"
                    for memory_id
                    in other_ids
                )

                proposal["blocked"] = True
                proposal["block_reason"] = (
                    "Same proposed semantic "
                    "key as "
                    f"{formatted_ids}. "
                    "Possible duplicate or "
                    "conflict."
                )

        self.memory.save_migration_plan(
            proposals
        )

        return proposals

    def _format_migration_plan(
        self,
        proposals,
    ):
        if not proposals:
            return (
                "No unkeyed legacy memories "
                "were found."
            )

        lines = [
            "Legacy memory migration preview:",
            "",
            (
                "PREVIEW ONLY - no memories "
                "have been changed."
            ),
            (
                "This exact plan has been saved "
                "and can be applied with "
                "/memory-migration-apply."
            ),
        ]

        for proposal in proposals:
            lines.append("")
            lines.append(
                f"Memory #{proposal['id']}"
            )

            lines.append(
                "Current: "
                f"[{proposal['current_type']}] "
                "[unkeyed]"
            )

            if (
                proposal["proposed_type"]
                and proposal["proposed_key"]
            ):
                lines.append(
                    "Proposed: "
                    f"[{proposal['proposed_type']}] "
                    f"[{proposal['proposed_key']}]"
                )
            else:
                lines.append(
                    "Proposed: "
                    "[REVIEW REQUIRED]"
                )

            lines.append(
                "Content: "
                f"{proposal['content']}"
            )

            if proposal["blocked"]:
                lines.append(
                    "BLOCKED: "
                    f"{proposal['block_reason']} "
                    "This memory will NOT be "
                    "changed automatically."
                )
            else:
                lines.append(
                    "Status: READY TO APPLY"
                )

        return "\n".join(
            lines
        )

    def _migration_preview(self):
        proposals = (
            self._build_migration_plan()
        )

        return self._format_migration_plan(
            proposals
        )

    def _migration_apply(self):
        plan = (
            self.memory.get_migration_plan()
        )

        if not plan:
            return (
                "There is no saved migration "
                "plan to apply. Run "
                "/memory-migration-preview "
                "first."
            )

        result = (
            self.memory.apply_migration_plan()
        )

        lines = [
            "Legacy migration finished."
        ]

        if result["applied"]:
            applied_ids = ", ".join(
                f"#{memory_id}"
                for memory_id
                in result["applied"]
            )

            lines.append(
                "Migrated: "
                f"{applied_ids}"
            )
        else:
            lines.append(
                "Migrated: none"
            )

        if result["blocked"]:
            blocked_ids = ", ".join(
                f"#{memory_id}"
                for memory_id
                in result["blocked"]
            )

            lines.append(
                "Blocked and unchanged: "
                f"{blocked_ids}"
            )

        if result["stale"]:
            stale_ids = ", ".join(
                f"#{memory_id}"
                for memory_id
                in result["stale"]
            )

            lines.append(
                "Skipped because the memory "
                "changed after preview: "
                f"{stale_ids}"
            )

        lines.append(
            "The saved migration plan has "
            "now been cleared."
        )

        return "\n".join(
            lines
        )

    def memory_command(self, user_input):
        """
        Run a memory command such as /memories or /forget and return
        its answer, or None when the text is not a memory command.
        """
        return self._handle_memory_command(user_input)

    def _handle_memory_command(
        self,
        user_input,
    ):
        stripped_input = user_input.strip()
        lower_input = stripped_input.lower()

        conversation_answer = self._conversation_command(
            stripped_input
        )

        if conversation_answer is not None:
            return conversation_answer

        if lower_input == "/context":
            report = self.context.last_report
            return json.dumps(report, ensure_ascii=False, indent=2) if report else "No conversation context has been built yet."

        if lower_input == "/memory-help":
            return (
                "Memory commands:\n"
                "/context - Show context sources, budget and omissions (no source content)\n"
                "/find <words> - Search memories and past conversations\n"
                "/history - List past conversations\n"
                "/history <number> - Read a past conversation\n"
                "/history forget <number>|all - Delete past conversations\n"
                "/memories - Show saved memories\n"
                "/remember <text> - Save a memory\n"
                "/edit <ID> <text> - Edit a memory\n"
                "/forget <ID> - Delete a memory\n"
                "/memory-history <ID> - Show memory history\n"
                "/memory-search <query> - Show raw semantic retrieval results\n"
                "/memory-rerank <query> - Preview relevance filtering\n"
                "/memory-llm-rerank <query> - Preview LLM relevance filtering\n"
                "/memory-restore <MEMORY_ID> <HISTORY_ID> - Restore a historical version\n"
                "/memory-compare <ID1> <ID2> - Compare two memories\n"
                "/memory-resolve-duplicate <KEEP_ID> <REMOVE_ID> - Resolve confirmed duplicates\n"
                "/memory-migration-preview - Build and preview migration plan\n"
                "/memory-migration-apply - Apply the saved safe migration plan\n"
                "/memory-help - Show these commands"
            )

        if lower_input.startswith(
            "/memory-llm-rerank "
        ):
            query = stripped_input[
                len("/memory-llm-rerank "):
            ].strip()

            if not query:
                return (
                    "Use /memory-llm-rerank "
                    "<query>."
                )

            return (
                self._memory_llm_rerank_diagnostic(
                    query
                )
            )

        if lower_input.startswith(
            "/memory-rerank "
        ):
            query = stripped_input[
                len("/memory-rerank "):
            ].strip()

            if not query:
                return "Use /memory-rerank <query>."

            return self._memory_rerank_diagnostic(
                query
            )

        if lower_input.startswith(
            "/memory-search "
        ):
            query = stripped_input[
                len("/memory-search "):
            ].strip()

            if not query:
                return (
                    "Use /memory-search "
                    "<query>."
                )

            return (
                self._memory_search_diagnostic(
                    query
                )
            )

        if lower_input.startswith(
            "/memory-restore "
        ):
            restore_text = stripped_input[
                len("/memory-restore "):
            ].strip()

            parts = restore_text.split()

            if len(parts) != 2:
                return (
                    "Use /memory-restore "
                    "<MEMORY_ID> <HISTORY_ID>."
                )

            try:
                memory_id = int(
                    parts[0]
                )

                history_id = int(
                    parts[1]
                )

            except ValueError:
                return (
                    "Please provide two numeric "
                    "IDs."
                )

            return self._restore_memory(
                memory_id,
                history_id,
            )

        if lower_input.startswith(
            "/memory-resolve-duplicate "
        ):
            resolution_text = stripped_input[
                len(
                    "/memory-resolve-duplicate "
                ):
            ].strip()

            parts = resolution_text.split()

            if len(parts) != 2:
                return (
                    "Use "
                    "/memory-resolve-duplicate "
                    "<KEEP_ID> <REMOVE_ID>."
                )

            try:
                keep_id = int(
                    parts[0]
                )

                remove_id = int(
                    parts[1]
                )

            except ValueError:
                return (
                    "Please provide two numeric "
                    "memory IDs."
                )

            return self._resolve_duplicate(
                keep_id,
                remove_id,
            )

        if lower_input.startswith(
            "/memory-compare "
        ):
            compare_text = stripped_input[
                len("/memory-compare "):
            ].strip()

            parts = compare_text.split()

            if len(parts) != 2:
                return (
                    "Use /memory-compare "
                    "<ID1> <ID2>."
                )

            try:
                memory_id_a = int(
                    parts[0]
                )

                memory_id_b = int(
                    parts[1]
                )

            except ValueError:
                return (
                    "Please provide two numeric "
                    "memory IDs."
                )

            return self._compare_memories(
                memory_id_a,
                memory_id_b,
            )

        if lower_input == (
            "/memory-migration-preview"
        ):
            return (
                self._migration_preview()
            )

        if lower_input == (
            "/memory-migration-apply"
        ):
            return (
                self._migration_apply()
            )

        if lower_input == "/memories":
            memories = (
                self.memory.get_all_memories()
            )

            if not memories:
                return (
                    "I don't have any saved "
                    "memories yet."
                )

            lines = ["Saved memories:"]

            for (
                memory_id,
                memory_type,
                memory_key,
                content,
                created_at,
                updated_at,
            ) in memories:

                display_key = (
                    memory_key
                    if memory_key
                    else "unkeyed"
                )

                lines.append(
                    f"[{memory_id}] "
                    f"[{memory_type}] "
                    f"[{display_key}] "
                    f"{content} "
                    f"(created: {created_at}, "
                    f"updated: {updated_at})"
                )

            return "\n".join(lines)

        if lower_input.startswith(
            "/memory-history "
        ):
            memory_id_text = (
                stripped_input[
                    len("/memory-history "):
                ].strip()
            )

            try:
                memory_id = int(
                    memory_id_text
                )
            except ValueError:
                return (
                    "Please provide a numeric "
                    "memory ID."
                )

            history = (
                self.memory.get_memory_history(
                    memory_id
                )
            )

            if not history:
                return (
                    f"Memory {memory_id} "
                    "doesn't have any archived "
                    "history yet."
                )

            lines = [
                f"History for memory {memory_id}:"
            ]

            for (
                history_id,
                archived_memory_id,
                action,
                memory_type,
                memory_key,
                content,
                original_created_at,
                original_updated_at,
                archived_at,
            ) in history:

                display_key = (
                    memory_key
                    if memory_key
                    else "unkeyed"
                )

                lines.append(
                    f"[History {history_id}] "
                    f"[{action}] "
                    f"[{memory_type}] "
                    f"[{display_key}] "
                    f"{content} "
                    f"(original created: "
                    f"{original_created_at}, "
                    f"original updated: "
                    f"{original_updated_at}, "
                    f"archived: {archived_at})"
                )

            return "\n".join(lines)

        if lower_input.startswith(
            "/remember "
        ):
            memory = stripped_input[
                len("/remember "):
            ].strip()

            if not memory:
                return (
                    "Tell me what you'd like "
                    "me to remember."
                )

            candidate = evaluate_memory(
                memory
            )

            if candidate is None:
                memory_id = (
                    self.memory.add_memory(
                        content=memory,
                    )
                )

                return (
                    f"I'll remember that. "
                    f"Memory ID: {memory_id}"
                )

            result = (
                self.memory.process_candidate(
                    candidate
                )
            )

            return (
                f"I'll remember that. "
                f"Memory ID: "
                f"{result['memory_id']}"
            )

        if lower_input.startswith(
            "/edit "
        ):
            edit_text = stripped_input[
                len("/edit "):
            ].strip()

            parts = edit_text.split(
                maxsplit=1
            )

            if len(parts) != 2:
                return (
                    "Use /edit <ID> "
                    "<new memory text>."
                )

            try:
                memory_id = int(
                    parts[0]
                )
            except ValueError:
                return (
                    "Please provide a numeric "
                    "memory ID."
                )

            new_content = parts[1].strip()

            if not new_content:
                return (
                    "Please provide the new "
                    "memory text."
                )

            existing = (
                self.memory.get_memory(
                    memory_id
                )
            )

            if existing is None:
                return (
                    f"I couldn't find memory "
                    f"{memory_id}."
                )

            candidate = evaluate_memory(
                new_content
            )

            if candidate:
                updated = (
                    self.memory.update_memory(
                        memory_id=memory_id,
                        content=candidate[
                            "content"
                        ],
                        memory_type=candidate[
                            "type"
                        ],
                        memory_key=candidate[
                            "key"
                        ],
                        action="manual_edit",
                    )
                )

            else:
                updated = (
                    self.memory.update_memory(
                        memory_id=memory_id,
                        content=new_content,
                        action="manual_edit",
                    )
                )

            if updated:
                return (
                    f"Memory {memory_id} "
                    "has been updated."
                )

            return (
                f"I couldn't update memory "
                f"{memory_id}."
            )

        if lower_input.startswith(
            "/forget "
        ):
            memory_id_text = (
                stripped_input[
                    len("/forget "):
                ].strip()
            )

            try:
                memory_id = int(
                    memory_id_text
                )

            except ValueError:
                return (
                    "Please provide the numeric "
                    "memory ID. Use /memories "
                    "to see the IDs."
                )

            deleted = (
                self.memory.delete_memory(
                    memory_id
                )
            )

            if deleted:
                return (
                    f"Memory {memory_id} "
                    "has been deleted."
                )

            return (
                f"I couldn't find memory "
                f"{memory_id}."
            )

        if lower_input.startswith(
            "remember "
        ):
            memory = stripped_input[
                len("remember "):
            ].strip()

            if memory:
                candidate = evaluate_memory(
                    memory
                )

                if candidate:
                    result = (
                        self.memory.process_candidate(
                            candidate
                        )
                    )

                    return (
                        "I'll remember that. "
                        f"Memory ID: "
                        f"{result['memory_id']}"
                    )

                memory_id = (
                    self.memory.add_memory(
                        content=memory
                    )
                )

                return (
                    "I'll remember that. "
                    f"Memory ID: {memory_id}"
                )

        return None
