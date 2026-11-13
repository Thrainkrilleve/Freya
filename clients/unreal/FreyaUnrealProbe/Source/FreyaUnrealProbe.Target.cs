using UnrealBuildTool;
public class FreyaUnrealProbeTarget : TargetRules
{
    public FreyaUnrealProbeTarget(TargetInfo Target) : base(Target)
    {
        Type = TargetType.Game;
        DefaultBuildSettings = BuildSettingsVersion.Latest;
        IncludeOrderVersion = EngineIncludeOrderVersion.Unreal5_8;
        ExtraModuleNames.Add("FreyaUnrealProbe");
    }
}
