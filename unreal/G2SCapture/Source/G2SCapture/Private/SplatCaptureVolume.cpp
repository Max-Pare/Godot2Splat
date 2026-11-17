#include "SplatCaptureVolume.h"

#include "Components/BoxComponent.h"
#include "Modules/ModuleManager.h"

ASplatCaptureVolume::ASplatCaptureVolume()
{
	PrimaryActorTick.bCanEverTick = false;
	bIsEditorOnlyActor = true;
	SetHidden(true);

	Box = CreateDefaultSubobject<UBoxComponent>(TEXT("Box"));
	Box->SetBoxExtent(FVector(500.f, 500.f, 200.f), false);
	Box->SetCollisionEnabled(ECollisionEnabled::NoCollision);
	Box->SetGenerateOverlapEvents(false);
	Box->SetCanEverAffectNavigation(false);
	Box->bHiddenInGame = true;
	Box->ShapeColor = FColor(80, 200, 255);
	Box->SetLineThickness(2.f);
	RootComponent = Box;
}

TArray<FVector> ASplatCaptureVolume::GetStartPoints() const
{
	TArray<FVector> Points;
	Points.Add(GetActorLocation());
	for (const FVector& Local : ExtraStartPoints)
	{
		Points.Add(GetActorTransform().TransformPosition(Local));
	}
	return Points;
}

IMPLEMENT_MODULE(FDefaultModuleImpl, G2SCapture)
