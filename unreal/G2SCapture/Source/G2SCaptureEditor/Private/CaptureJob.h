#pragma once

#include "CoreMinimal.h"
#include "Containers/Ticker.h"
#include "ViewPlanner.h"

class ASplatCaptureVolume;
class USceneCaptureComponent2D;
class UTextureRenderTarget2D;
class UWorld;
class AActor;

DECLARE_LOG_CATEGORY_EXTERN(LogG2S, Log, All);

/**
 * Scans the editor's level for the g2s pipeline: places cameras in the free space
 * of a SplatCaptureVolume and saves a colour picture and a depth map per view.
 * A port of godot/addons/godot2splat/capture/capture_runner.gd.
 *
 * Output folder (read by src/g2s/stages/ingest.py), in Godot's conventions so that
 * the rest of the pipeline is the same for both engines:
 *   capture.json      settings and one entry per view; transforms in Godot axes, metres
 *   images/NAME.png   colour as the game shows it (tonemapped, sRGB)
 *   depth/NAME.bin    half-float RGB: depth in whole millimetres, split as
 *                     R = (mm div 2048) / 2048, G = (mm mod 2048) / 2048, B = metres / far
 *   sky/NAME.png      the sky alone, six views
 *
 * The job runs one step per editor frame (FTSTicker) so that temporal
 * anti-aliasing and Lumen see frames go by, like a game would.
 */
class FG2SCaptureJob : public TSharedFromThis<FG2SCaptureJob>
{
public:
	DECLARE_DELEGATE_OneParam(FOnFinished, int32 /*ExitCode*/);

	FG2SCaptureJob(UWorld* InWorld, ASplatCaptureVolume* InVolume, const FString& InOutDir, const FString& InMapPath);
	~FG2SCaptureJob();

	void Start(FOnFinished InOnFinished);
	void Cancel();
	bool IsRunning() const { return TickHandle.IsValid(); }
	FText GetProgress() const { return FText::FromString(Progress); }

	/** The volume to scan: the selected one, else the only one in the world. Null with a reason otherwise.
	 *  In a World Partition level, the region around a volume that is not loaded yet is loaded first. */
	static ASplatCaptureVolume* FindVolume(UWorld* World, FString& OutError);

	/**
	 * A temporary volume (not saved with the level) at the actor with this label or
	 * name, with default settings. The actor itself is left out of the pictures.
	 * In a World Partition level, the region around it is loaded.
	 */
	static ASplatCaptureVolume* MakeTemporaryVolume(UWorld* World, const FString& Origin, FString& OutError);

	/** Sets properties of `Object` from "Name=Value;Name=Value" (Unreal's text format). Returns an error or "". */
	static FString ApplySettings(UObject* Object, const FString& Settings);

	/** Destroy the volume when the scan ends (a temporary one). */
	void SetOwnsVolume(bool bOwns) { bOwnsVolume = bOwns; }

private:
	enum class EPhase { Settle, Meter, Plan, Shoot, Sky, Done };

	static void LoadRegion(UWorld* World, const FBox& Box);

	bool Tick(float DeltaTime);
	void Finish(int32 Code);
	void Fail(const FString& Message);
	void Note(const FString& Text);

	void BuildCaptures();
	USceneCaptureComponent2D* NewCapture(int32 Size, bool bDepth, bool bHdr);
	void ConfigureColour(USceneCaptureComponent2D* Capture);
	void FindSky();
	void UseLevelLighting();

	bool TickSettle();
	bool TickMeter();
	bool TickPlan();
	bool FindLimits();
	bool InsideLimits(const FVector& At) const;
	bool TickShoot();
	bool TickSky();
	bool WriteManifest();

	G2SPlanner::FProbe Probe(const FVector& At);
	bool SaveColour(USceneCaptureComponent2D* Capture, const FString& Path);
	bool SaveDepth(USceneCaptureComponent2D* Capture, const FString& Path);
	void PlaceShot(int32 Index);

	TWeakObjectPtr<UWorld> World;
	TWeakObjectPtr<ASplatCaptureVolume> Volume;
	FString OutDir;
	FString MapPath;
	FOnFinished OnFinished;
	FTSTicker::FDelegateHandle TickHandle;
	double StartSeconds = 0.0;
	FString Progress;
	TArray<FString> Notes;

	EPhase Phase = EPhase::Settle;
	int32 Counter = 0;
	bool bWasDirty = false;
	TArray<TPair<FString, FString>> SavedCVars;
	bool bSavedThrottle = true;

	TWeakObjectPtr<AActor> Rig;
	TObjectPtr<USceneCaptureComponent2D> Colour = nullptr;
	TObjectPtr<USceneCaptureComponent2D> Depth = nullptr;
	TArray<TObjectPtr<USceneCaptureComponent2D>> ProbeCaptures;
	TArray<TObjectPtr<USceneCaptureComponent2D>> MeterCaptures;
	TArray<TWeakObjectPtr<AActor>> Hidden;
	float Near = 5.f;
	int32 DepthSize = 0;
	bool bManualSceneExposure = false;
	float ExposureBias = 0.f;

	TArray<FQuat> Faces = G2SPlanner::FaceRotations();
	// the volume's box; for Fill, the span of the positions found (reported only)
	bool bOwnsVolume = false;
	FTransform BoxXf;
	FVector BoxExtent = FVector::ZeroVector;
	// planning
	G2SPlanner::FGrid Grid;
	TSet<FIntVector> Seen;
	int32 Outdoors = 0;
	TArray<FVector> Queue;
	int32 Seeds = 0;
	int32 Next = 0;
	int32 TooClose = 0;
	TArray<FVector> Positions;
	// GaussianVoxelLimit volumes: cameras only inside them (none: no limit)
	struct FLimit
	{
		TWeakObjectPtr<class AVolume> Volume;
		FBox Bounds;
		bool bBoundsOnly = false;   // no collision shape to test against: its bounding box is used
	};
	TArray<FLimit> Limits;
	int32 OutsideLimits = 0;
	// pictures
	struct FShot { FString Name; FString Split; FQuat Rotation; FVector Location; };
	TArray<FShot> Shots;
	TArray<FShot> SkyShots;
	int32 ShotIndex = 0;
	TArray<TWeakObjectPtr<class UPrimitiveComponent>> SkyComponents;
};
