import unreal, traceback
OUT = unreal.Paths.project_saved_dir() + "ue_level_out.txt"
open(OUT, "w").close()
def say(*a):
    unreal.log("FREYALEVEL " + " ".join(str(x) for x in a))
    with open(OUT, "a", encoding="utf-8") as f: f.write(" ".join(str(x) for x in a) + "\n")
try:
    les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    MAP = "/Game/Maps/ProbeLevel"
    if unreal.EditorAssetLibrary.does_asset_exist(MAP):
        say("level exists; loading", les.load_level(MAP))
        for a in eas.get_all_level_actors():
            if a.get_class().get_name() not in ("WorldSettings", "WorldDataLayers", "Brush", "DefaultPhysicsVolume"):
                eas.destroy_actor(a)
    else:
        say("new level", les.new_level(MAP))
    cls = unreal.load_object(None, "/Game/Unpacked/Freya/BP_Freya.BP_Freya_C")
    freya = eas.spawn_actor_from_class(cls, unreal.Vector(0, 0, 0), unreal.Rotator(0, 0, 0))
    freya.set_actor_label("Freya")
    for name, value in (("UseLiveLink", True), ("LiveLinkSubject", unreal.LiveLinkSubjectName("FreyaAudioProbe"))):
        try:
            freya.set_editor_property(name, value); say("set", name, "->", freya.get_editor_property(name))
        except Exception as e:
            say("could not set", name, str(e)[:200])
    for name in ("UseARKit", "LiveLinkSubject", "UseLiveLink"):
        try: say(name, "=", freya.get_editor_property(name))
        except Exception as e: say(name, "ERR", str(e)[:120])

    sds = unreal.get_engine_subsystem(unreal.SubobjectDataSubsystem)
    root = sds.k2_gather_subobject_data_for_instance(freya)[0]
    params = unreal.AddNewSubobjectParams(parent_handle=root, new_class=unreal.FreyaAudioProbeComponent, blueprint_context=None)
    handle, reason = sds.add_new_subobject(params)
    say("probe component added", unreal.SubobjectDataBlueprintFunctionLibrary.is_handle_valid(handle), reason)
    probes = freya.get_components_by_class(unreal.FreyaAudioProbeComponent)
    say("probe components on actor", len(probes), [p.get_editor_property("fixture_path") for p in probes])

    origin, extent = freya.get_actor_bounds(False)
    say("bounds origin", origin, "extent", extent, "forward", freya.get_actor_forward_vector())
    top = origin.z + extent.z
    head = unreal.Vector(origin.x, origin.y, top - 12.0)
    # MetaHumans face +Y. The camera stands in front, at head height.
    cam = eas.spawn_actor_from_class(unreal.CameraActor, unreal.Vector(head.x, head.y + 125.0, head.z - 6.0), unreal.Rotator(0, 0, -90))
    cam.set_actor_label("ConversationCamera")
    cam.set_actor_rotation(unreal.MathLibrary.find_look_at_rotation(cam.get_actor_location(), unreal.Vector(head.x, head.y, head.z - 6.0)), False)
    cam.set_editor_property("auto_activate_for_player", unreal.AutoReceiveInput.PLAYER0)
    cc = cam.get_component_by_class(unreal.CameraComponent); cc.set_editor_property("field_of_view", 30.0); cc.set_editor_property("constrain_aspect_ratio", False)

    sun = eas.spawn_actor_from_class(unreal.DirectionalLight, unreal.Vector(0, 0, 300), unreal.Rotator(-35, 0, -110)); sun.set_actor_label("KeyLight")
    sun.get_component_by_class(unreal.DirectionalLightComponent).set_editor_property("intensity", 3.0)
    sky = eas.spawn_actor_from_class(unreal.SkyLight, unreal.Vector(0, 0, 300)); sky.set_actor_label("FillLight")
    sc = sky.get_component_by_class(unreal.SkyLightComponent)
    sc.set_editor_property("source_type", unreal.SkyLightSourceType.SLS_SPECIFIED_CUBEMAP)
    cube = unreal.load_asset("/Engine/EngineResources/GrayLightTextureCube")
    say("fill cubemap", cube)
    if cube: sc.set_editor_property("cubemap", cube)
    sc.set_editor_property("intensity", 1.2)
    # A fixed exposure, so the picture does not brighten itself against the empty background.
    ppv = eas.spawn_actor_from_class(unreal.PostProcessVolume, unreal.Vector(0, 0, 0)); ppv.set_actor_label("FixedExposure")
    ppv.set_editor_property("unbound", True)
    st = ppv.get_editor_property("settings")
    for key, value in (("auto_exposure_min_brightness", 1.0), ("auto_exposure_max_brightness", 1.0), ("auto_exposure_bias", 0.0)):
        st.set_editor_property("override_" + key, True); st.set_editor_property(key, value)
    ppv.set_editor_property("settings", st)
    say("saved level", les.save_current_level())
    say("saved dirty", unreal.EditorLoadingAndSavingUtils.save_dirty_packages(True, True))
    say("actors", [(a.get_actor_label(), a.get_class().get_name()) for a in eas.get_all_level_actors()])
    say("DONE")
except Exception:
    say("FAILED", traceback.format_exc())
unreal.SystemLibrary.quit_editor()
