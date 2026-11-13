using UnrealBuildTool;
public class FreyaUnrealProbeEditorTarget : TargetRules
{
    public FreyaUnrealProbeEditorTarget(TargetInfo Target) : base(Target)
    {
        Type = TargetType.Editor;
        DefaultBuildSettings = BuildSettingsVersion.Latest;
        IncludeOrderVersion = EngineIncludeOrderVersion.Unreal5_8;
        ExtraModuleNames.Add("FreyaUnrealProbe");
    }
}
