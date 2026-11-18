#include "CaptureJob.h"

#include "AssetCompilingManager.h"
#include "Components/BoxComponent.h"
#include "Components/PrimitiveComponent.h"
#include "Components/SceneCaptureComponent2D.h"
#include "Dom/JsonObject.h"
#include "Editor.h"
#include "Editor/EditorPerformanceSettings.h"
#include "Engine/Selection.h"
#include "Engine/TextureRenderTarget2D.h"
#include "Engine/World.h"
#include "EngineUtils.h"
#include "HAL/FileManager.h"
#include "HAL/IConsoleManager.h"
#include "ImageCore.h"
#include "ImageUtils.h"
#include "Interfaces/Interface_PostProcessVolume.h"
#include "LevelEditorViewport.h"
#include "Materials/Material.h"
#include "Materials/MaterialInterface.h"
#include "Misc/FileHelper.h"
#include "Misc/Paths.h"
#include "Policies/PrettyJsonPrintPolicy.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"
#include "ShaderCompiler.h"
#include "SplatCaptureVolume.h"
#include "TextureResource.h"
#include "UObject/UnrealType.h"
#include "WorldPartition/LoaderAdapter/LoaderAdapterShape.h"
#include "WorldPartition/WorldPartition.h"
#include "WorldPartition/WorldPartitionActorDescInstance.h"
#include "WorldPartition/WorldPartitionEditorLoaderAdapter.h"
#include "WorldPartition/WorldPartitionHelpers.h"

DEFINE_LOG_CATEGORY(LogG2S);

namespace
{
	constexpr int32 ProbeSize = 64;
	constexpr float Fov = 90.f;
	const TCHAR* FaceNames[] = {TEXT("px"), TEXT("nx"), TEXT("py"), TEXT("ny"), TEXT("pz"), TEXT("nz")};
	// Depth split, as in geometry.decode_depth: each part is exact in a half float.
	constexpr int32 DepthSplit = 2048;
	// World Partition: what is loaded beyond the scanned area.
	constexpr float LoadMargin = 10000.f;

	TSharedRef<FJsonValue> Numbers(const TArray<double>& Values)
	{
		TArray<TSharedPtr<FJsonValue>> Out;
		for (double V : Values)
		{
			Out.Add(MakeShared<FJsonValueNumber>(V));
		}
		return MakeShared<FJsonValueArray>(Out);
	}

	FText ViewportOverrideName()
	{
		return NSLOCTEXT("G2SCapture", "ViewportOverride", "Scan to Splat");
	}

	float Luminance(const FLinearColor& C)
	{
		return 0.2126f * C.R + 0.7152f * C.G + 0.0722f * C.B;
	}

	/**
	 * The level's auto exposure settings for a view at a point: the project defaults,
	 * then every post process volume there, blended the way the renderer does it
	 * (UWorld::AddPostProcessingSettings, FSceneView::OverridePostProcessSettings): in
	 * order of priority, unbound volumes first among equals, each weighted by its blend
	 * weight and blend radius. Only the auto exposure fields; the scan needs no others.
	 * Min and max brightness are returned in EV100 whatever the project setting.
	 */
	FPostProcessSettings LevelExposureAt(UWorld* W, const FVector& At)
	{
		struct FEntry
		{
			FPostProcessVolumeProperties Props;
			float Weight;
		};
		TArray<FEntry> Entries;
		for (IInterface_PostProcessVolume& PPV : W->GetPostProcessVolumeIterator())
		{
			const FPostProcessVolumeProperties Props = PPV.GetProperties();
			if (!Props.bIsEnabled || Props.Settings == nullptr)
			{
				continue;
			}
			float Weight = FMath::Clamp(Props.BlendWeight, 0.f, 1.f);
			if (!Props.bIsUnbound)
			{
				float Distance = 0.f;
				PPV.EncompassesPoint(At, 0.f, &Distance);
				if (Distance < 0.f || Distance > Props.BlendRadius)
				{
					continue;
				}
				if (Props.BlendRadius >= 1.f)
				{
					Weight *= 1.f - Distance / Props.BlendRadius;
				}
			}
			if (Weight > 0.f)
			{
				Entries.Add({Props, Weight});
			}
		}
		Entries.StableSort([](const FEntry& A, const FEntry& B)
		{
			return A.Props.Priority != B.Props.Priority ? A.Props.Priority < B.Props.Priority
				: A.Props.bIsUnbound && !B.Props.bIsUnbound;
		});

		FPostProcessSettings Out;  // the project's defaults
		for (const FEntry& E : Entries)
		{
			const FPostProcessSettings& S = *E.Props.Settings;
			if (S.bOverride_AutoExposureMethod) Out.AutoExposureMethod = S.AutoExposureMethod;
			if (S.bOverride_AutoExposureApplyPhysicalCameraExposure) Out.AutoExposureApplyPhysicalCameraExposure = S.AutoExposureApplyPhysicalCameraExposure;
			if (S.bOverride_AutoExposureBias) Out.AutoExposureBias = FMath::Lerp(Out.AutoExposureBias, S.AutoExposureBias, E.Weight);
			if (S.bOverride_AutoExposureMinBrightness) Out.AutoExposureMinBrightness = FMath::Lerp(Out.AutoExposureMinBrightness, S.AutoExposureMinBrightness, E.Weight);
			if (S.bOverride_AutoExposureMaxBrightness) Out.AutoExposureMaxBrightness = FMath::Lerp(Out.AutoExposureMaxBrightness, S.AutoExposureMaxBrightness, E.Weight);
		}
		IConsoleVariable* Extended = IConsoleManager::Get().FindConsoleVariable(TEXT("r.DefaultFeature.AutoExposure.ExtendDefaultLuminanceRange"));
		if (Extended == nullptr || Extended->GetInt() == 0)
		{
			// Without the extended range the limits are white point luminances.
			float LensAttenuation = 0.78f;
			if (IConsoleVariable* CVar = IConsoleManager::Get().FindConsoleVariable(TEXT("r.EyeAdaptation.LensAttenuation")))
			{
				LensAttenuation = CVar->GetFloat();
			}
			const float LuminanceMax = 0.78f / FMath::Max(LensAttenuation, 0.01f);
			Out.AutoExposureMinBrightness = FMath::Log2(FMath::Max(Out.AutoExposureMinBrightness, 1e-6f) / LuminanceMax);
			Out.AutoExposureMaxBrightness = FMath::Log2(FMath::Max(Out.AutoExposureMaxBrightness, 1e-6f) / LuminanceMax);
		}
		return Out;
	}
}

FG2SCaptureJob::FG2SCaptureJob(UWorld* InWorld, ASplatCaptureVolume* InVolume, const FString& InOutDir, const FString& InMapPath)
	: World(InWorld), Volume(InVolume), OutDir(InOutDir), MapPath(InMapPath)
{
}

FG2SCaptureJob::~FG2SCaptureJob()
{
	if (TickHandle.IsValid())
	{
		FTSTicker::GetCoreTicker().RemoveTicker(TickHandle);
	}
}

ASplatCaptureVolume* FG2SCaptureJob::FindVolume(UWorld* World, FString& OutError)
{
	if (World == nullptr)
	{
		OutError = TEXT("no level is open");
		return nullptr;
	}
	TArray<ASplatCaptureVolume*> Found;
	for (TActorIterator<ASplatCaptureVolume> It(World); It; ++It)
	{
		Found.Add(*It);
	}
	if (Found.Num() == 0 && World->GetWorldPartition() != nullptr)
	{
		// World Partition: the volume may sit in a region that is not loaded.
		TArray<FBox> Regions;
		FWorldPartitionHelpers::ForEachActorDescInstance<ASplatCaptureVolume>(World->GetWorldPartition(), [&Regions](const FWorldPartitionActorDescInstance* Desc)
		{
			Regions.Add(Desc->GetEditorBounds());
			return true;
		});
		for (const FBox& Region : Regions)
		{
			LoadRegion(World, Region.ExpandBy(LoadMargin));
		}
		for (TActorIterator<ASplatCaptureVolume> It(World); It; ++It)
		{
			Found.Add(*It);
		}
	}
	if (Found.Num() == 0)
	{
		OutError = TEXT("no Splat Capture Volume in the level. Add one inside the area to scan, with its origin in open air.");
		return nullptr;
	}
	if (Found.Num() > 1 && GEditor != nullptr)
	{
		for (ASplatCaptureVolume* V : Found)
		{
			if (GEditor->GetSelectedActors()->IsSelected(V))
			{
				return V;
			}
		}
		UE_LOG(LogG2S, Warning, TEXT("capture: %d Splat Capture Volumes and none selected; using %s"), Found.Num(), *Found[0]->GetActorNameOrLabel());
	}
	return Found[0];
}

void FG2SCaptureJob::LoadRegion(UWorld* World, const FBox& Box)
{
	UWorldPartition* Partition = World->GetWorldPartition();
	if (Partition == nullptr || !Box.IsValid)
	{
		return;
	}
	UWorldPartitionEditorLoaderAdapter* Adapter = Partition->CreateEditorLoaderAdapter<FLoaderAdapterShape>(World, Box, TEXT("g2s scan"));
	Adapter->GetLoaderAdapter()->Load();
	UE_LOG(LogG2S, Display, TEXT("capture: loaded the World Partition region %s"), *Box.ToString());
}

ASplatCaptureVolume* FG2SCaptureJob::MakeTemporaryVolume(UWorld* World, const FString& Origin, FString& OutError)
{
	auto Find = [World, &Origin]() -> AActor*
	{
		for (TActorIterator<AActor> It(World); It; ++It)
		{
			if (It->GetActorLabel() == Origin || It->GetName() == Origin)
			{
				return *It;
			}
		}
		return nullptr;
	};
	// The fill reaches at most this far from the actor (the volume's default Fill Reach).
	const FVector Reach(GetDefault<ASplatCaptureVolume>()->FillReach + LoadMargin);
	AActor* Start = Find();
	if (Start == nullptr && World->GetWorldPartition() != nullptr)
	{
		// Not loaded: find it among the level's actor descriptions and load around it.
		FBox Bounds(ForceInit);
		bool bFound = false;
		FWorldPartitionHelpers::ForEachActorDescInstance(World->GetWorldPartition(), AActor::StaticClass(), [&](const FWorldPartitionActorDescInstance* Desc)
		{
			if (Desc->GetActorLabelString() == Origin || Desc->GetActorNameString() == Origin)
			{
				Bounds = Desc->GetEditorBounds();
				bFound = true;
				return false;
			}
			return true;
		});
		if (bFound)
		{
			// An actor without visible parts may have no bounds: then load the whole level.
			LoadRegion(World, Bounds.IsValid ? Bounds.ExpandBy(Reach) : World->GetWorldPartition()->GetEditorWorldBounds());
			Start = Find();
		}
	}
	if (Start == nullptr)
	{
		OutError = FString::Printf(TEXT("no actor labelled or named %s in the level"), *Origin);
		return nullptr;
	}
	if (World->GetWorldPartition() != nullptr)
	{
		const FVector At = Start->GetActorLocation();
		LoadRegion(World, FBox(At, At).ExpandBy(Reach));
	}

	FActorSpawnParameters Params;
	Params.ObjectFlags = RF_Transient;
	Params.bTemporaryEditorActor = true;
	Params.bHideFromSceneOutliner = true;
	Params.Name = MakeUniqueObjectName(World->PersistentLevel, ASplatCaptureVolume::StaticClass(), TEXT("G2STemporaryVolume"));
	ASplatCaptureVolume* Volume = World->SpawnActor<ASplatCaptureVolume>(ASplatCaptureVolume::StaticClass(),
		FTransform(FRotator(0.f, Start->GetActorRotation().Yaw, 0.f), Start->GetActorLocation()), Params);
	if (Volume == nullptr)
	{
		OutError = TEXT("cannot place a temporary capture volume");
		return nullptr;
	}
	Volume->Exclude.Add(Start);
	UE_LOG(LogG2S, Display, TEXT("capture: start point %s at %s"), *Start->GetActorLabel(), *Start->GetActorLocation().ToCompactString());
	return Volume;
}

FString FG2SCaptureJob::ApplySettings(UObject* Object, const FString& Settings)
{
	TArray<FString> Items;
	Settings.ParseIntoArray(Items, TEXT(";"), true);
	for (const FString& Item : Items)
	{
		FString Name, Value;
		if (!Item.Split(TEXT("="), &Name, &Value))
		{
			return FString::Printf(TEXT("setting '%s' is not Name=Value"), *Item);
		}
		Name.TrimStartAndEndInline();
		Value.TrimStartAndEndInline();
		FProperty* Property = Object->GetClass()->FindPropertyByName(*Name);
		if (Property == nullptr)
		{
			return FString::Printf(TEXT("the capture volume has no setting '%s'"), *Name);
		}
		if (Property->ImportText_InContainer(*Value, Object, Object, PPF_None) == nullptr)
		{
			return FString::Printf(TEXT("'%s' is not a valid value for %s"), *Value, *Name);
		}
		UE_LOG(LogG2S, Display, TEXT("capture: %s = %s"), *Name, *Value);
	}
	return FString();
}

void FG2SCaptureJob::Start(FOnFinished InOnFinished)
{
	OnFinished = InOnFinished;
	StartSeconds = FPlatformTime::Seconds();
	UWorld* W = World.Get();
	ASplatCaptureVolume* V = Volume.Get();
	if (W == nullptr || V == nullptr)
	{
		Fail(TEXT("the level or the volume is gone"));
		return;
	}
	bWasDirty = W->PersistentLevel->GetOutermost()->IsDirty();
	BoxXf = FTransform(V->Box->GetComponentQuat(), V->Box->GetComponentLocation());
	BoxExtent = V->Box->GetScaledBoxExtent();

	IFileManager& Files = IFileManager::Get();
	Files.DeleteDirectory(*OutDir, false, true);
	for (const TCHAR* Folder : {TEXT("images"), TEXT("depth"), TEXT("sky")})
	{
		if (!Files.MakeDirectory(*FPaths::Combine(OutDir, Folder), true))
		{
			Fail(FString::Printf(TEXT("cannot create %s"), *OutDir));
			return;
		}
	}

	// Full-resolution textures for every view, and a full frame rate when the
	// editor window is in the background (the command line runs it that way).
	for (const TCHAR* Name : {TEXT("r.Streaming.FullyLoadUsedTextures")})
	{
		if (IConsoleVariable* CVar = IConsoleManager::Get().FindConsoleVariable(Name))
		{
			SavedCVars.Add({Name, CVar->GetString()});
			CVar->Set(TEXT("1"), ECVF_SetByCode);
		}
	}
	UEditorPerformanceSettings* Perf = GetMutableDefault<UEditorPerformanceSettings>();
	bSavedThrottle = Perf->bThrottleCPUWhenNotForeground;
	Perf->bThrottleCPUWhenNotForeground = false;
	// The pictures come from the scan's own scene captures, not the viewports: stop
	// the viewports drawing every frame so the GPU goes to the scan. A temporary
	// override, removed at the end; the user's own Realtime setting is not changed.
	if (GEditor != nullptr)
	{
		for (FLevelEditorViewportClient* Client : GEditor->GetLevelViewportClients())
		{
			Client->AddRealtimeOverride(false, ViewportOverrideName());
		}
	}

	FAssetCompilingManager::Get().FinishAllCompilation();
	if (GShaderCompilingManager != nullptr)
	{
		GShaderCompilingManager->FinishAllCompilation();
	}

	for (const TSoftObjectPtr<AActor>& Soft : V->Exclude)
	{
		if (AActor* A = Soft.Get())
		{
			Hidden.Add(A);
		}
		else
		{
			UE_LOG(LogG2S, Warning, TEXT("capture: WARNING exclude entry not found: %s"), *Soft.ToString());
		}
	}
	BuildCaptures();
	Phase = EPhase::Settle;
	Counter = 0;
	Progress = TEXT("loading");
	TickHandle = FTSTicker::GetCoreTicker().AddTicker(FTickerDelegate::CreateSP(this, &FG2SCaptureJob::Tick));
}

void FG2SCaptureJob::Cancel()
{
	if (IsRunning())
	{
		Fail(TEXT("stopped"));
	}
}

void FG2SCaptureJob::Fail(const FString& Message)
{
	UE_LOG(LogG2S, Error, TEXT("capture: ERROR %s"), *Message);
	Finish(1);
}

void FG2SCaptureJob::Note(const FString& Text)
{
	Notes.Add(Text);
	UE_LOG(LogG2S, Display, TEXT("capture: NOTE %s"), *Text);
}

void FG2SCaptureJob::Finish(int32 Code)
{
	if (TickHandle.IsValid())
	{
		FTSTicker::GetCoreTicker().RemoveTicker(TickHandle);
		TickHandle.Reset();
	}
	if (AActor* R = Rig.Get())
	{
		R->Destroy();
	}
	Rig.Reset();
	if (bOwnsVolume && Volume.IsValid())
	{
		Volume->Destroy();
	}
	if (UWorld* W = World.Get())
	{
		// The temporary camera rig is not a change to the level.
		W->PersistentLevel->GetOutermost()->SetDirtyFlag(bWasDirty);
	}
	for (const TPair<FString, FString>& Saved : SavedCVars)
	{
		if (IConsoleVariable* CVar = IConsoleManager::Get().FindConsoleVariable(*Saved.Key))
		{
			CVar->Set(*Saved.Value, ECVF_SetByCode);
		}
	}
	SavedCVars.Empty();
	GetMutableDefault<UEditorPerformanceSettings>()->bThrottleCPUWhenNotForeground = bSavedThrottle;
	if (GEditor != nullptr)
	{
		for (FLevelEditorViewportClient* Client : GEditor->GetLevelViewportClients())
		{
			if (Client->HasRealtimeOverride(ViewportOverrideName()))
			{
				Client->RemoveRealtimeOverride(ViewportOverrideName());
			}
		}
	}
	Phase = EPhase::Done;
	OnFinished.ExecuteIfBound(Code);
}

// --- captures ---------------------------------------------------------------

USceneCaptureComponent2D* FG2SCaptureJob::NewCapture(int32 Size, bool bDepth, bool bHdr)
{
	AActor* R = Rig.Get();
	UTextureRenderTarget2D* Target = NewObject<UTextureRenderTarget2D>(R);
	Target->RenderTargetFormat = bDepth ? RTF_R32f : (bHdr ? RTF_RGBA16f : RTF_RGBA8);
	Target->ClearColor = FLinearColor::Black;
	Target->bAutoGenerateMips = false;
	Target->InitAutoFormat(Size, Size);
	Target->UpdateResourceImmediate(true);

	USceneCaptureComponent2D* Capture = NewObject<USceneCaptureComponent2D>(R);
	Capture->bCaptureEveryFrame = false;
	Capture->bCaptureOnMovement = false;
	Capture->FOVAngle = Fov;
	Capture->TextureTarget = Target;
	Capture->bOverride_CustomNearClippingPlane = true;
	Capture->CustomNearClippingPlane = Near;
	Capture->CaptureSource = bDepth ? SCS_SceneDepth : (bHdr ? SCS_SceneColorHDRNoAlpha : SCS_FinalColorLDR);
	for (const TWeakObjectPtr<AActor>& A : Hidden)
	{
		if (A.IsValid())
		{
			Capture->HiddenActors.Add(A.Get());
		}
	}
	if (bDepth)
	{
		// Depth must sit on the pixel centres: no sub-pixel jitter.
		Capture->ShowFlags.SetAntiAliasing(false);
		Capture->ShowFlags.SetTemporalAA(false);
	}
	Capture->SetupAttachment(R->GetRootComponent());
	Capture->RegisterComponent();
	return Capture;
}

void FG2SCaptureJob::BuildCaptures()
{
	UWorld* W = World.Get();
	ASplatCaptureVolume* V = Volume.Get();
	Near = FMath::Min(5.f, V->MinClearance * 0.25f);

	FActorSpawnParameters Params;
	Params.ObjectFlags = RF_Transient;
	Params.bTemporaryEditorActor = true;
	Params.bHideFromSceneOutliner = true;
	Params.Name = MakeUniqueObjectName(W->PersistentLevel, AActor::StaticClass(), TEXT("G2SCaptureRig"));
	AActor* R = W->SpawnActor<AActor>(AActor::StaticClass(), FTransform::Identity, Params);
	USceneComponent* Root = NewObject<USceneComponent>(R, TEXT("Root"));
	R->SetRootComponent(Root);
	Root->RegisterComponent();
	Rig = R;

	// Colour: the level as the game shows it. Its rendering state persists between
	// frames so that temporal anti-aliasing and Lumen converge during the warmup.
	Colour = NewCapture(V->ImageSize, false, false);
	Colour->bAlwaysPersistRenderingState = true;
	ConfigureColour(Colour);

	// Depth: the same view, along the view axis in cm, at half size.
	DepthSize = FMath::Max(64, V->ImageSize / 2);
	Depth = NewCapture(DepthSize, true, false);
	for (int32 I = 0; I < 6; ++I)
	{
		ProbeCaptures.Add(NewCapture(ProbeSize, true, false));
	}
	// Exposure meter: the six directions around the start point, before tonemapping.
	for (int32 I = 0; I < 6; ++I)
	{
		USceneCaptureComponent2D* Meter = NewCapture(ProbeSize, false, true);
		Meter->bAlwaysPersistRenderingState = true;
		MeterCaptures.Add(Meter);
	}
}

void FG2SCaptureJob::ConfigureColour(USceneCaptureComponent2D* Capture)
{
	ASplatCaptureVolume* V = Volume.Get();
	if (V->bKeepScreenSpaceEffects)
	{
		return;
	}
	FEngineShowFlags& Flags = Capture->ShowFlags;
	Flags.SetMotionBlur(false);
	Flags.SetDepthOfField(false);
	Flags.SetBloom(false);
	Flags.SetLensFlares(false);
	Flags.SetScreenSpaceReflections(false);
	Flags.SetAmbientOcclusion(false);
	Flags.SetVignette(false);
	Flags.SetGrain(false);
	Flags.SetSceneColorFringe(false);
	Note(TEXT("switched off for the scan (they differ from view to view): screen-space reflections, ambient occlusion (SSAO), "
		"bloom, lens flares, depth of field, motion blur, vignette, grain, chromatic aberration. "
		"Turn on Keep Screen Space Effects on the volume to keep them."));
}

void FG2SCaptureJob::FindSky()
{
	// The sky picture shows only the sky: Sky Atmosphere and fog draw anyway, sky
	// meshes are chosen by their material's Is Sky flag or by being larger than the far clip.
	UWorld* W = World.Get();
	const float Far = Volume->FarClip;
	for (TActorIterator<AActor> It(W); It; ++It)
	{
		if (Hidden.Contains(*It) || *It == Rig.Get())
		{
			continue;
		}
		TArray<UPrimitiveComponent*> Prims;
		It->GetComponents(Prims);
		for (UPrimitiveComponent* P : Prims)
		{
			if (!P->IsRegistered() || !P->IsVisible())
			{
				continue;
			}
			bool bSky = P->Bounds.SphereRadius > Far;
			for (int32 I = 0; !bSky && I < P->GetNumMaterials(); ++I)
			{
				UMaterialInterface* M = P->GetMaterial(I);
				bSky = M != nullptr && M->GetMaterial() != nullptr && M->GetMaterial()->bIsSky;
			}
			if (bSky)
			{
				SkyComponents.Add(P);
			}
		}
	}
}

// --- frames ----------------------------------------------------------------

bool FG2SCaptureJob::Tick(float DeltaTime)
{
	if (!World.IsValid() || !Volume.IsValid() || !Rig.IsValid())
	{
		Fail(TEXT("the level was closed or the volume deleted during the scan"));
		return false;
	}
	bool bContinue = true;
	switch (Phase)
	{
	case EPhase::Settle: bContinue = TickSettle(); break;
	case EPhase::Meter: bContinue = TickMeter(); break;
	case EPhase::Plan: bContinue = TickPlan(); break;
	case EPhase::Shoot: bContinue = TickShoot(); break;
	case EPhase::Sky: bContinue = TickSky(); break;
	default: return false;
	}
	return bContinue && Phase != EPhase::Done;
}

bool FG2SCaptureJob::TickSettle()
{
	if (GShaderCompilingManager != nullptr && GShaderCompilingManager->IsCompiling())
	{
		return true;
	}
	if (++Counter < Volume->SettleFrames)
	{
		return true;
	}
	Counter = 0;
	Phase = EPhase::Meter;
	return true;
}

bool FG2SCaptureJob::TickMeter()
{
	ASplatCaptureVolume* V = Volume.Get();
	UWorld* W = World.Get();
	const FVector At = V->GetActorLocation();

	// What the level's own auto exposure would do at the start point.
	const FPostProcessSettings Level = LevelExposureAt(W, At);
	// Manual exposure in the level already fixes it for every view.
	bManualSceneExposure = Level.AutoExposureMethod == AEM_Manual;
	if (bManualSceneExposure)
	{
		Note(TEXT("exposure: the level's post process volume sets Manual exposure; used as it is."));
		Phase = EPhase::Plan;
		return true;
	}

	float EV100 = V->ExposureEV100;
	float Measured = EV100;
	if (!V->bOverrideExposure)
	{
		if (Counter == 0)
		{
			for (int32 I = 0; I < 6; ++I)
			{
				MeterCaptures[I]->SetWorldLocationAndRotation(At, Faces[I]);
			}
		}
		if (Counter++ < V->WarmupFrames)
		{
			for (USceneCaptureComponent2D* Meter : MeterCaptures)
			{
				Meter->CaptureScene();
			}
			return true;
		}
		// Like Unreal's histogram auto exposure: the mean log luminance between the
		// 10th and 90th percentile is shown as middle grey (0.18).
		TArray<float> Logs;
		for (USceneCaptureComponent2D* Meter : MeterCaptures)
		{
			TArray<FLinearColor> Pixels;
			Meter->TextureTarget->GameThread_GetRenderTargetResource()->ReadLinearColorPixels(Pixels);
			for (const FLinearColor& C : Pixels)
			{
				Logs.Add(FMath::Log2(FMath::Max(Luminance(C), 1e-6f)));
			}
		}
		Logs.Sort();
		double Sum = 0.0;
		const int32 Lo = Logs.Num() / 10, Hi = FMath::Max(Lo + 1, Logs.Num() * 9 / 10);
		for (int32 I = Lo; I < Hi; ++I)
		{
			Sum += Logs[I];
		}
		const float AverageLuminance = FMath::Pow(2.f, float(Sum / (Hi - Lo)));
		float LensAttenuation = 0.78f;
		if (IConsoleVariable* CVar = IConsoleManager::Get().FindConsoleVariable(TEXT("r.EyeAdaptation.LensAttenuation")))
		{
			LensAttenuation = CVar->GetFloat();
		}
		const float LuminanceMax = 0.78f / FMath::Max(LensAttenuation, 0.01f);
		Measured = FMath::Log2(AverageLuminance / (0.18f * LuminanceMax));
		// The level's Min and Max EV100 hold auto exposure in a range. Dark levels
		// often set it so that they stay dark: without it, the scan comes out
		// several stops brighter than the game.
		EV100 = FMath::Clamp(Measured, FMath::Min(Level.AutoExposureMinBrightness, Level.AutoExposureMaxBrightness),
			Level.AutoExposureMaxBrightness);
	}
	// Manual exposure without the physical camera: EV100 0, shifted by the bias.
	const float Compensation = Level.AutoExposureBias + V->ExposureCompensation;
	ExposureBias = Compensation - EV100;
	FPostProcessSettings& PP = Colour->PostProcessSettings;
	PP.bOverride_AutoExposureMethod = true;
	PP.AutoExposureMethod = AEM_Manual;
	PP.bOverride_AutoExposureApplyPhysicalCameraExposure = true;
	PP.AutoExposureApplyPhysicalCameraExposure = false;
	PP.bOverride_AutoExposureBias = true;
	PP.AutoExposureBias = ExposureBias;
	Colour->PostProcessBlendWeight = 1.f;
	FString Source = TEXT("set on the volume");
	if (!V->bOverrideExposure)
	{
		Source = TEXT("measured at the start point");
		if (!FMath::IsNearlyEqual(Measured, EV100, 0.005f))
		{
			Source += FString::Printf(TEXT(" as %.2f, held to the level's range %.2f to %.2f"), Measured,
				Level.AutoExposureMinBrightness, Level.AutoExposureMaxBrightness);
		}
	}
	Note(FString::Printf(TEXT("exposure fixed for every picture at EV100 %.2f (%s), compensation %+.2f (level %+.2f, volume %+.2f)."),
		EV100, *Source, Compensation, Level.AutoExposureBias, V->ExposureCompensation));
	for (USceneCaptureComponent2D* Meter : MeterCaptures)
	{
		Meter->DestroyComponent();
	}
	MeterCaptures.Empty();
	Counter = 0;
	Phase = EPhase::Plan;
	return true;
}

G2SPlanner::FProbe FG2SCaptureJob::Probe(const FVector& At)
{
	for (int32 I = 0; I < 6; ++I)
	{
		ProbeCaptures[I]->SetWorldLocationAndRotation(At, Faces[I]);
		ProbeCaptures[I]->CaptureScene();
	}
	G2SPlanner::FProbe Out;
	for (int32 I = 0; I < 6; ++I)
	{
		TArray<FLinearColor> Pixels;
		ProbeCaptures[I]->TextureTarget->GameThread_GetRenderTargetResource()->ReadLinearColorPixels(Pixels);
		Out.Add(G2SPlanner::FaceDistances(Pixels, ProbeSize, Volume->FarClip));
	}
	return Out;
}

bool FG2SCaptureJob::TickPlan()
{
	// Walks outward from the start points through everything they can see: a grid
	// cell is reached when an accepted position sees it. Box: only cells inside the
	// box. Fill: any cell within Fill Reach, except that cells which see mostly sky
	// (outdoors) are not taken and the walk does not go on from them.
	ASplatCaptureVolume* V = Volume.Get();
	const float Spacing = V->CameraSpacing;
	const bool bFill = V->Placement == ESplatCapturePlacement::Fill;
	if (Queue.Num() == 0)
	{
		Grid = bFill ? G2SPlanner::FGrid::Fill(FTransform(V->GetActorQuat(), V->GetActorLocation()), Spacing)
			: G2SPlanner::FGrid::Box(BoxXf, BoxExtent, Spacing);
		Queue = V->GetStartPoints();
		Seeds = Queue.Num();
		Next = 0;
	}

	// As many probes as fit in a short slice of this frame: the editor stays responsive.
	const double SliceEnd = FPlatformTime::Seconds() + 0.05;
	while (Next < Queue.Num() && FPlatformTime::Seconds() < SliceEnd)
	{
		const FVector At = Queue[Next++];
		const G2SPlanner::FProbe P = Probe(At);
		const float Room = G2SPlanner::Clearance(P);
		if (Room < V->MinClearance)
		{
			if (Next <= Seeds)
			{
				Fail(FString::Printf(TEXT("start point %s is %.0f cm from a surface (Min Clearance is %.0f cm). "
					"Move the Splat Capture Volume or its extra start point into open air."), *At.ToCompactString(), Room, V->MinClearance));
				return false;
			}
			++TooClose;
			continue;
		}
		if (bFill && Next > Seeds && G2SPlanner::SkyShare(P) > V->OutdoorSkyShare)
		{
			++Outdoors;
			continue;
		}
		Positions.Add(At);
		const FIntVector Here = Grid.CellOf(At);
		const int32 R = FMath::CeilToInt(2.5f * Spacing / FMath::Min3(Grid.Step.X, Grid.Step.Y, Grid.Step.Z)) + 1;
		for (int32 DX = -R; DX <= R; ++DX)
		{
			for (int32 DY = -R; DY <= R; ++DY)
			{
				for (int32 DZ = -R; DZ <= R; ++DZ)
				{
					const FIntVector C = Here + FIntVector(DX, DY, DZ);
					if (!Grid.Contains(C) || Seen.Contains(C))
					{
						continue;
					}
					const FVector To = Grid.Position(C);
					if (FVector::Dist(At, To) > Spacing * 2.5f
						|| (bFill && FVector::Dist(V->GetActorLocation(), To) > V->FillReach)
						|| !G2SPlanner::Sees(P, Faces, ProbeSize, At, To, 5.f))
					{
						continue;
					}
					Seen.Add(C);
					Queue.Add(To);
				}
			}
		}
	}
	Progress = FString::Printf(TEXT("finding free space: %d camera positions"), Positions.Num());
	if (Next < Queue.Num())
	{
		return true;
	}

	if (bFill)
	{
		const FBox Reach(Positions);
		const FVector Size = Reach.GetSize() / 100.0;
		UE_LOG(LogG2S, Display, TEXT("capture: the open space reachable from the start point spans %.1f x %.1f x %.1f m (Unreal X, Y, Z): "
			"%d positions %.2f m apart (%d too close to a surface, %d outdoors)"),
			Size.X, Size.Y, Size.Z, Positions.Num(), Spacing / 100.f, TooClose, Outdoors);
		// What the manifest reports as the scanned box.
		BoxXf = FTransform(Reach.GetCenter());
		BoxExtent = Reach.GetExtent();
	}
	else
	{
		UE_LOG(LogG2S, Display, TEXT("capture: %d of %lld grid points are in reachable open space (%d too close to a surface)"),
			Positions.Num() - Seeds, Grid.NumCells(), TooClose);
	}
	const int32 MaxPositions = V->MaxPositions > 0 ? V->MaxPositions : G2SPlanner::AutoMaxPositions(V->ImageSize);
	if (Positions.Num() > MaxPositions)
	{
		UE_LOG(LogG2S, Display, TEXT("capture: thinned evenly to %d positions (%s). %s"), MaxPositions,
			V->MaxPositions > 0 ? TEXT("Max Positions") : TEXT("what training can hold at this Image Size"),
			V->MaxPositions > 0 ? TEXT("Raise it or the camera spacing to change this.")
				: TEXT("A smaller Image Size allows more positions; so does a machine with more memory and a higher Max Positions."));
		TArray<FVector> Thinned;
		for (int32 I : G2SPlanner::Spread(Positions, MaxPositions, Seeds))
		{
			Thinned.Add(Positions[I]);
		}
		Positions = Thinned;
	}

	// The six faces of a cube see every direction; turning the cube at random
	// keeps the picture edges from lining up between positions.
	FRandomStream Rng(2);
	for (int32 P = 0; P < Positions.Num(); ++P)
	{
		const FQuat Turn = G2SPlanner::RandomRotation(Rng);
		const bool bTest = V->TestEvery > 0 && P % V->TestEvery == V->TestEvery - 1;
		for (int32 F = 0; F < 6; ++F)
		{
			Shots.Add({FString::Printf(TEXT("%04d_%d"), P, F), bTest ? TEXT("test") : TEXT("train"), Turn * Faces[F], Positions[P]});
		}
	}
	for (int32 F = 0; F < 6; ++F)
	{
		SkyShots.Add({FaceNames[F], TEXT(""), Faces[F], V->GetActorLocation()});
	}
	for (USceneCaptureComponent2D* C : ProbeCaptures)
	{
		C->DestroyComponent();
	}
	ProbeCaptures.Empty();
	ShotIndex = 0;
	Counter = 0;
	Phase = EPhase::Shoot;
	return true;
}

bool FG2SCaptureJob::SaveColour(USceneCaptureComponent2D* Capture, const FString& Path)
{
	TArray<FColor> Pixels;
	UTextureRenderTarget2D* Target = Capture->TextureTarget;
	if (!Target->GameThread_GetRenderTargetResource()->ReadPixels(Pixels))
	{
		return false;
	}
	for (FColor& C : Pixels)
	{
		C.A = 255;
	}
	return FImageUtils::SaveImageByExtension(*Path, FImageView(Pixels.GetData(), Target->SizeX, Target->SizeY, EGammaSpace::sRGB));
}

bool FG2SCaptureJob::SaveDepth(USceneCaptureComponent2D* Capture, const FString& Path)
{
	TArray<FLinearColor> Pixels;
	if (!Capture->TextureTarget->GameThread_GetRenderTargetResource()->ReadLinearColorPixels(Pixels))
	{
		return false;
	}
	const double FarMm = Volume->FarClip * 10.0;
	TArray<uint16> Raw;
	Raw.SetNumUninitialized(Pixels.Num() * 3);
	for (int32 I = 0; I < Pixels.Num(); ++I)
	{
		const double Cm = Pixels[I].R;
		const double Mm = (Cm > 0.0 && Cm * 10.0 < FarMm) ? FMath::RoundToDouble(Cm * 10.0) : 0.0;
		const double High = FMath::FloorToDouble(Mm / DepthSplit);
		Raw[I * 3 + 0] = FFloat16(float(High / DepthSplit)).Encoded;
		Raw[I * 3 + 1] = FFloat16(float((Mm - High * DepthSplit) / DepthSplit)).Encoded;
		Raw[I * 3 + 2] = FFloat16(float(Mm / FarMm)).Encoded;
	}
	return FFileHelper::SaveArrayToFile(TArrayView<const uint8>((const uint8*)Raw.GetData(), Raw.Num() * sizeof(uint16)), *Path);
}

void FG2SCaptureJob::PlaceShot(int32 Index)
{
	const FShot& Shot = Phase == EPhase::Sky ? SkyShots[Index] : Shots[Index];
	Colour->SetWorldLocationAndRotation(Shot.Location, Shot.Rotation);
	Depth->SetWorldLocationAndRotation(Shot.Location, Shot.Rotation);
}

bool FG2SCaptureJob::TickShoot()
{
	ASplatCaptureVolume* V = Volume.Get();
	if (Counter == 0)
	{
		PlaceShot(ShotIndex);
	}
	Colour->CaptureScene();
	if (++Counter < V->WarmupFrames)
	{
		return true;
	}
	Depth->CaptureScene();
	const FShot& Shot = Shots[ShotIndex];
	if (!SaveColour(Colour, FPaths::Combine(OutDir, TEXT("images"), Shot.Name + TEXT(".png")))
		|| !SaveDepth(Depth, FPaths::Combine(OutDir, TEXT("depth"), Shot.Name + TEXT(".bin"))))
	{
		Fail(FString::Printf(TEXT("cannot write to %s"), *OutDir));
		return false;
	}
	Counter = 0;
	++ShotIndex;
	const int32 Position = ShotIndex / 6;
	if (ShotIndex % 6 == 0)
	{
		Progress = FString::Printf(TEXT("taking pictures: position %d of %d"), Position, Positions.Num());
		if (Position % 10 == 0 || Position == Positions.Num())
		{
			UE_LOG(LogG2S, Display, TEXT("capture: position %d of %d"), Position, Positions.Num());
		}
	}
	if (ShotIndex == Shots.Num())
	{
		// The sky alone, for the background shown behind the splat.
		FindSky();
		Colour->PrimitiveRenderMode = ESceneCapturePrimitiveRenderMode::PRM_UseShowOnlyList;
		Colour->ShowOnlyComponents = SkyComponents;
		ShotIndex = 0;
		Phase = EPhase::Sky;
	}
	return true;
}

bool FG2SCaptureJob::TickSky()
{
	if (Counter == 0)
	{
		PlaceShot(ShotIndex);
	}
	Colour->CaptureScene();
	if (++Counter < Volume->WarmupFrames)
	{
		return true;
	}
	if (!SaveColour(Colour, FPaths::Combine(OutDir, TEXT("sky"), SkyShots[ShotIndex].Name + TEXT(".png"))))
	{
		Fail(FString::Printf(TEXT("cannot write to %s"), *OutDir));
		return false;
	}
	Counter = 0;
	if (++ShotIndex < SkyShots.Num())
	{
		return true;
	}
	if (!WriteManifest())
	{
		return false;
	}
	UE_LOG(LogG2S, Display, TEXT("capture: done in %.0f s"), FPlatformTime::Seconds() - StartSeconds);
	Finish(0);
	return false;
}

bool FG2SCaptureJob::WriteManifest()
{
	ASplatCaptureVolume* V = Volume.Get();
	auto ViewList = [](const TArray<FShot>& List, bool bSplit)
	{
		TArray<TSharedPtr<FJsonValue>> Out;
		for (const FShot& S : List)
		{
			TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
			O->SetStringField(TEXT("name"), S.Name);
			if (bSplit)
			{
				O->SetStringField(TEXT("split"), S.Split);
			}
			O->SetField(TEXT("transform"), Numbers(G2SPlanner::GodotNumbers(S.Rotation, S.Location)));
			Out.Add(MakeShared<FJsonValueObject>(O));
		}
		return Out;
	};
	const FVector GodotSize = G2SPlanner::ToGodotDirection(BoxExtent).GetAbs() * 2.0 / 100.0;

	TSharedRef<FJsonObject> VolumeJson = MakeShared<FJsonObject>();
	VolumeJson->SetField(TEXT("transform"), Numbers(G2SPlanner::GodotNumbers(BoxXf.GetRotation(), BoxXf.GetLocation())));
	VolumeJson->SetField(TEXT("size"), Numbers({GodotSize.X, GodotSize.Y, GodotSize.Z}));
	TSharedRef<FJsonObject> Training = MakeShared<FJsonObject>();
	Training->SetNumberField(TEXT("steps"), V->TrainingSteps);
	Training->SetNumberField(TEXT("max_gaussians"), V->MaxGaussians);
	TArray<TSharedPtr<FJsonValue>> NoteValues;
	for (const FString& N : Notes)
	{
		NoteValues.Add(MakeShared<FJsonValueString>(N));
	}

	TSharedRef<FJsonObject> M = MakeShared<FJsonObject>();
	M->SetNumberField(TEXT("version"), 1);
	M->SetStringField(TEXT("engine"), TEXT("unreal"));
	M->SetStringField(TEXT("scene"), MapPath);
	M->SetStringField(TEXT("output_dir"), V->OutputDir);
	M->SetStringField(TEXT("godot_project"), V->GodotProject.Path);
	M->SetField(TEXT("image_size"), Numbers({double(V->ImageSize), double(V->ImageSize)}));
	M->SetField(TEXT("depth_size"), Numbers({double(DepthSize), double(DepthSize)}));
	M->SetNumberField(TEXT("fov_y"), Fov);
	M->SetNumberField(TEXT("near"), Near / 100.0);
	M->SetNumberField(TEXT("far"), V->FarClip / 100.0);
	M->SetNumberField(TEXT("positions"), Positions.Num());
	M->SetObjectField(TEXT("volume"), VolumeJson);
	// The volume actor's position (the start actor's, for a volume placed by
	// -G2SOrigin): export puts it at the Godot origin. Under Fill the box above is
	// the reached space, centred elsewhere.
	const FVector Start = G2SPlanner::ToGodotDirection(V->GetActorLocation()) / 100.0;
	M->SetField(TEXT("start"), Numbers({Start.X, Start.Y, Start.Z}));
	M->SetObjectField(TEXT("training"), Training);
	M->SetArrayField(TEXT("notes"), NoteValues);
	M->SetArrayField(TEXT("views"), ViewList(Shots, true));
	M->SetArrayField(TEXT("sky"), ViewList(SkyShots, false));

	FString Text;
	TSharedRef<TJsonWriter<TCHAR, TPrettyJsonPrintPolicy<TCHAR>>> Writer = TJsonWriterFactory<TCHAR, TPrettyJsonPrintPolicy<TCHAR>>::Create(&Text);
	FJsonSerializer::Serialize(M, Writer);
	if (!FFileHelper::SaveStringToFile(Text, *FPaths::Combine(OutDir, TEXT("capture.json")), FFileHelper::EEncodingOptions::ForceUTF8WithoutBOM))
	{
		Fail(FString::Printf(TEXT("cannot write capture.json to %s"), *OutDir));
		return false;
	}
	return true;
}
