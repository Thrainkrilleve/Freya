"""
Minecraft companion, milestone M1.

Importing this package does not start a process and does not register
a capability. The control plane does not import it.
"""

from freya.player.minecraft.adapter import MinecraftAdapter, register_minecraft

__all__ = ["MinecraftAdapter", "register_minecraft"]
