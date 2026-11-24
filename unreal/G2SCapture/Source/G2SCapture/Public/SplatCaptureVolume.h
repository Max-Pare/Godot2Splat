#pragma once

#include "CoreMinimal.h"
#include "Engine/EngineTypes.h"
#include "GameFramework/Actor.h"
#include "SplatCaptureVolume.generated.h"

class UBoxComponent;

UENUM()
enum class ESplatCapturePlacement : uint8
{
	/** Cameras fill the open space reachable from the start points, however far it goes (up to Fill Reach). The box is not used. */
	Fill UMETA(DisplayName = "Fill the open space"),
	/** Cameras are placed in the reachable open space inside the box only. */
	Box UMETA(DisplayName = "Inside the box"),
};

/**
 * Marks the part of a level that gets scanned into a Gaussian splat.
 *
 * The box limits where cameras are placed. What lies outside the box is still
 * captured when it can be seen from inside. The actor's position is the start
 * point of the camera placement and has to be in open air, at least Min Clearance
 * from any surface. Extra Start Points add more, for example in a room behind a
 * closed door.
 *
 * A volume in the level labelled GaussianVoxelLimit (or GaussianVoxelLimit2, ...)
 * keeps the cameras inside it, whatever the placement: the fill never goes past it.
 *
 * Scan with the "Scan to Splat" toolbar button, or with
 * `g2s build <Unreal project> <map> --godot-project <Godot project>`.
 * The splat ends up in the Godot project, not in this one.
 */
UCLASS(meta = (DisplayName = "Splat Capture Volume"),
	hidecategories = (Collision, Physics, Input, LOD, Cooking, Replication, Networking, HLOD, Rendering, Navigation))
class G2SCAPTURE_API ASplatCaptureVolume : public AActor
{
	GENERATED_BODY()

public:
	ASplatCaptureVolume();

	/** Box in which cameras are placed with Placement "Inside the box". Its extent is half the box size. */
	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Splat Capture")
	TObjectPtr<UBoxComponent> Box;

	/**
	 * Where cameras go. Fill: the open space is filled cell by cell (Camera Spacing
	 * apart) from the start points through everything they can see, so a whole
	 * interior is scanned without sizing a box. The fill does not go out through
	 * windows and doors into the open: a cell that sees mostly sky stops it.
	 */
	UPROPERTY(EditAnywhere, Category = "Splat Capture")
	ESplatCapturePlacement Placement = ESplatCapturePlacement::Fill;

	/** Fill: cameras stay within this distance of the actor. */
	UPROPERTY(EditAnywhere, Category = "Splat Capture", meta = (EditCondition = "Placement == ESplatCapturePlacement::Fill", ClampMin = "500", ClampMax = "100000", Units = "cm"))
	float FillReach = 15000.f;

	/** Fill: a cell whose surroundings are more than this share sky counts as outdoors and stops the fill. 1 never stops. */
	UPROPERTY(EditAnywhere, Category = "Splat Capture", meta = (EditCondition = "Placement == ESplatCapturePlacement::Fill", ClampMin = "0.05", ClampMax = "1"))
	float OutdoorSkyShare = 0.3f;

	/** More start points, relative to this actor. Each must be in open air. */
	UPROPERTY(EditAnywhere, Category = "Splat Capture", meta = (MakeEditWidget = true))
	TArray<FVector> ExtraStartPoints;

	/** Distance between camera positions. Each position gives 6 pictures. */
	UPROPERTY(EditAnywhere, Category = "Splat Capture", meta = (ClampMin = "25", ClampMax = "500", Units = "cm"))
	float CameraSpacing = 100.f;

	/** Cameras keep at least this distance from every surface. */
	UPROPERTY(EditAnywhere, Category = "Splat Capture", meta = (ClampMin = "5", ClampMax = "200", Units = "cm"))
	float MinClearance = 30.f;

	/**
	 * Upper limit on camera positions; more than this are thinned out evenly.
	 * 0: as many as training can hold in memory (1.5 gigapixels of pictures: 390
	 * positions at 800 px, 610 at 640 px).
	 */
	UPROPERTY(EditAnywhere, Category = "Splat Capture", meta = (ClampMin = "0", ClampMax = "5000"))
	int32 MaxPositions = 0;

	/** Width and height of each picture in pixels. Training time follows the pixel count. */
	UPROPERTY(EditAnywhere, Category = "Splat Capture", meta = (ClampMin = "128", ClampMax = "2048"))
	int32 ImageSize = 800;

	/** Every n-th camera position is kept out of training and used to score the result. 0 holds nothing out. */
	UPROPERTY(EditAnywhere, Category = "Splat Capture", meta = (ClampMin = "0", ClampMax = "50"))
	int32 TestEvery = 10;

	/**
	 * Frames rendered at each view before the picture is taken. Temporal
	 * anti-aliasing and Lumen settle over these frames after each camera move.
	 */
	UPROPERTY(EditAnywhere, Category = "Splat Capture|Rendering", meta = (ClampMin = "1", ClampMax = "120"))
	int32 WarmupFrames = 16;

	/** Editor frames before the scan starts, for shaders, meshes and textures still loading. */
	UPROPERTY(EditAnywhere, Category = "Splat Capture|Rendering", meta = (ClampMin = "0", ClampMax = "600"))
	int32 SettleFrames = 30;

	/** Surfaces farther than this count as sky. */
	UPROPERTY(EditAnywhere, Category = "Splat Capture|Rendering", meta = (ClampMin = "1000", ClampMax = "1000000", Units = "cm"))
	float FarClip = 50000.f;

	/** Actors hidden during the scan (a player model, moving objects). */
	UPROPERTY(EditAnywhere, Category = "Splat Capture|Rendering")
	TArray<TSoftObjectPtr<AActor>> Exclude;

	/**
	 * Leave screen-space reflections, ambient occlusion, bloom, lens flares, depth
	 * of field, motion blur, vignette, grain and chromatic aberration on. They
	 * differ from view to view, which blurs the splat, so they are off by default.
	 */
	UPROPERTY(EditAnywhere, Category = "Splat Capture|Rendering")
	bool bKeepScreenSpaceEffects = false;

	/**
	 * Exposure is the same for every picture. By default it is measured once at the
	 * start point, as the level's auto exposure would set it there (within the Min and
	 * Max EV100 of its post process volumes), unless a post process volume sets
	 * Manual exposure (then that is used). Turn this on to give the EV100 yourself.
	 */
	UPROPERTY(EditAnywhere, Category = "Splat Capture|Rendering", meta = (InlineEditConditionToggle))
	bool bOverrideExposure = false;

	UPROPERTY(EditAnywhere, Category = "Splat Capture|Rendering", meta = (EditCondition = "bOverrideExposure", UIMin = "-10", UIMax = "20"))
	float ExposureEV100 = 10.f;

	/**
	 * Added to the level's own exposure compensation at the start point (its post
	 * process volumes' Exposure Compensation, 1 by default), in stops.
	 */
	UPROPERTY(EditAnywhere, Category = "Splat Capture|Rendering", meta = (UIMin = "-5", UIMax = "5"))
	float ExposureCompensation = 0.f;

	/** Upper limit on Gaussians. 0 sizes it to the scan. */
	UPROPERTY(EditAnywhere, Category = "Splat Capture|Training", meta = (ClampMin = "0", ClampMax = "6000000"))
	int32 MaxGaussians = 0;

	/** Training steps. 0 sizes it to the scan. */
	UPROPERTY(EditAnywhere, Category = "Splat Capture|Training", meta = (ClampMin = "0", ClampMax = "100000"))
	int32 TrainingSteps = 0;

	/** Godot project (the folder holding project.godot) that receives the splat scene. */
	UPROPERTY(EditAnywhere, Category = "Splat Capture|Output")
	FDirectoryPath GodotProject;

	/** Folder in the Godot project for the splat and its scene. Empty: res://splats/<map name>/ */
	UPROPERTY(EditAnywhere, Category = "Splat Capture|Output")
	FString OutputDir;

	/** All start points in world space: the actor's position first. */
	TArray<FVector> GetStartPoints() const;
};
