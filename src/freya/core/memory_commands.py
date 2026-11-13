"""
The owner's memory commands (/memories, /forget, /remember, the
migration and diagnostic commands and the rest), as part of
FreyaAssistant. Moved out of assistant.py unchanged, so that file
holds the turn itself.
"""
import json

from freya.core.llm import (
    classify_legacy_memory,
    compare_memories,
    evaluate_memory,
    rerank_memory_candidates,
)


class MemoryCommands:
    """
    Memory administration for FreyaAssistant. It relies on the
    assistant's memory, messages and conversation store.
    """

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
