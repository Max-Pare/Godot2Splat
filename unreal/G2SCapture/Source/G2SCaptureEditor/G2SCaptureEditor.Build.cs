using UnrealBuildTool;

public class G2SCaptureEditor : ModuleRules
{
	public G2SCaptureEditor(ReadOnlyTargetRules Target) : base(Target)
	{
		PCHUsage = PCHUsageMode.UseExplicitOrSharedPCHs;
		PrivateDependencyModuleNames.AddRange(new string[]
		{
			"Core", "CoreUObject", "Engine", "RenderCore", "RHI", "ImageCore", "Json",
			"UnrealEd", "LevelEditor", "ToolMenus", "Slate", "SlateCore", "Projects",
			"G2SCapture",
		});
	}
}
