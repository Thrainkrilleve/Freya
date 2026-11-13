import unreal, traceback
OUT = unreal.Paths.project_saved_dir() + "ue_assemble_out.txt"
def say(*a):
    line = " ".join(str(x) for x in a)
    unreal.log("FREYAASSEMBLE " + line)
    with open(OUT, "a", encoding="utf-8") as f: f.write(line + "\n")
try:
    ch = unreal.load_asset("/Game/Freya")
    sub = unreal.get_editor_subsystem(unreal.MetaHumanCharacterEditorSubsystem)
    say("loaded", ch is not None)
    say("add_to_edit", sub.try_add_object_to_edit(ch))
    if not sub.can_build_meta_human(ch, True):
        p = unreal.MetaHumanCharacterAutoRiggingRequestParams()
        say("rig params", [n for n in dir(p) if not n.startswith("_") and n not in ("assign","cast","copy","export_text","import_text","static_struct","to_dict","to_tuple")])
        p.set_editor_property("blocking", True)
        say("requesting rig, type", p.get_editor_property("rig_type"))
        sub.request_auto_rigging(ch, p)
        say("rig request returned")
    if not sub.can_build_meta_human(ch, True):
        t = unreal.MetaHumanCharacterTextureRequestParams()
        t.set_editor_property("blocking", True)
        say("requesting texture sources; has high res", ch.has_high_resolution_textures)
        sub.request_texture_sources(ch, t)
        say("texture request returned; has high res", ch.has_high_resolution_textures)
    ok = sub.can_build_meta_human(ch, True)
    say("can_build", ok)
    say("saved after rig/textures", unreal.EditorLoadingAndSavingUtils.save_dirty_packages(True, True))
    if ok:
        b = unreal.MetaHumanCharacterEditorBuildParameters()
        b.set_editor_property("pipeline_type", unreal.MetaHumanDefaultPipelineType.OPTIMIZED)
        say("build params", b.to_dict() if hasattr(b, "to_dict") else "")
        sub.build_meta_human(ch, b)
        say("build returned")
        say("saved dirty", unreal.EditorLoadingAndSavingUtils.save_dirty_packages(True, True))
        reg = unreal.AssetRegistryHelpers.get_asset_registry()
        assets = reg.get_assets_by_path("/Game", recursive=True)
        bps = [str(a.package_name) for a in assets if "Blueprint" in str(a.asset_class_path.asset_name)]
        say("assets", len(assets), "blueprints", bps)
    say("DONE")
except Exception:
    say("FAILED", traceback.format_exc())
unreal.SystemLibrary.quit_editor()
