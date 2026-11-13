using UnrealBuildTool;
public class FreyaUnrealProbe : ModuleRules
{
    public FreyaUnrealProbe(ReadOnlyTargetRules Target) : base(Target)
    {
        PCHUsage = PCHUsageMode.UseExplicitOrSharedPCHs;
        PublicDependencyModuleNames.AddRange(new string[] { "Core", "CoreUObject", "Engine", "ApplicationCore" });
        PrivateDependencyModuleNames.AddRange(new string[] {
            "LiveLink", "LiveLinkInterface", "MetaHumanLocalLiveLinkSource",
            "MetaHumanPipelineCore", "MetaHumanCoreTech",
            "WebSockets", "Json", "Slate", "SlateCore", "InputCore"
        });
    }
}
