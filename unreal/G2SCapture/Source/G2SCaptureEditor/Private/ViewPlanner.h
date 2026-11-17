#pragma once

#include "CoreMinimal.h"

/**
 * Where to put the capture cameras: the maths, without any rendering. A port of
 * godot/addons/godot2splat/capture/view_planner.gd, in Unreal units (cm).
 *
 * Free space is found from rendered depth alone, so it works with anything that
 * draws and needs no collision. A "probe" is what one point sees in every
 * direction: six square 90 degree depth views (one per cube face), each turned
 * into the distance to the nearest surface per pixel (0 = nothing there).
 * The capture job walks outward from the start points: a candidate position is
 * reached when an accepted position sees it. A point inside a wall or a solid
 * object is hidden from every point in open air, so it is never reached.
 *
 * Camera axes are Unreal's: X forward, Y right, Z up. Pixel row 0 is the top.
 */
namespace G2SPlanner
{
	using FProbe = TArray<TArray<float>, TFixedAllocator<6>>;

	/** Camera orientations of the six cube faces: looking along +X, -X, +Y, -Y, +Z, -Z. */
	inline TArray<FQuat> FaceRotations()
	{
		auto Make = [](const FVector& Forward, const FVector& Up)
		{
			return FRotationMatrix::MakeFromXZ(Forward, Up).ToQuat();
		};
		return {
			Make(FVector::ForwardVector, FVector::UpVector),
			Make(FVector::BackwardVector, FVector::UpVector),
			Make(FVector::RightVector, FVector::UpVector),
			Make(FVector::LeftVector, FVector::UpVector),
			Make(FVector::UpVector, FVector::BackwardVector),
			Make(FVector::DownVector, FVector::ForwardVector),
		};
	}

	/**
	 * Candidate camera positions: one per cell of a grid, each moved a little at
	 * random (the same way every time) so that rows of cameras do not line up with
	 * the scene.
	 *
	 * Box: the cells fill the box of half size `Extent` placed by `Xf`, `Spacing`
	 * apart (rounded so that they fit the box exactly). Fill: the grid has no end;
	 * cell 0 is centred on `Xf`'s origin, the start point. Cells are found on
	 * demand, so an unbounded grid costs nothing until the walk reaches it.
	 */
	struct FGrid
	{
		FTransform Xf;
		FVector Offset = FVector::ZeroVector;
		FVector Step = FVector(100.0);
		bool bBounded = false;
		FIntVector Count = FIntVector::ZeroValue;

		static FGrid Box(const FTransform& InXf, const FVector& Extent, float Spacing)
		{
			FGrid G;
			G.Xf = InXf;
			G.bBounded = true;
			G.Count = FIntVector(
				FMath::Max(1, FMath::RoundToInt(Extent.X * 2.0 / Spacing)),
				FMath::Max(1, FMath::RoundToInt(Extent.Y * 2.0 / Spacing)),
				FMath::Max(1, FMath::RoundToInt(Extent.Z * 2.0 / Spacing)));
			G.Step = Extent * 2.0 / FVector(G.Count);
			G.Offset = -Extent;
			return G;
		}

		static FGrid Fill(const FTransform& InXf, float Spacing)
		{
			FGrid G;
			G.Xf = InXf;
			G.Step = FVector(Spacing);
			G.Offset = -G.Step * 0.5;
			return G;
		}

		bool Contains(const FIntVector& C) const
		{
			return !bBounded || (C.X >= 0 && C.Y >= 0 && C.Z >= 0 && C.X < Count.X && C.Y < Count.Y && C.Z < Count.Z);
		}

		FVector Position(const FIntVector& C) const
		{
			FRandomStream Rng(int32(HashCombine(GetTypeHash(C), 0x9e3779b9u)));
			const FVector Jitter(Rng.FRandRange(-0.3f, 0.3f), Rng.FRandRange(-0.3f, 0.3f), Rng.FRandRange(-0.3f, 0.3f));
			return Xf.TransformPosition(Offset + (FVector(C) + FVector(0.5) + Jitter) * Step);
		}

		FIntVector CellOf(const FVector& World) const
		{
			const FVector L = (Xf.InverseTransformPosition(World) - Offset) / Step;
			return FIntVector(FMath::FloorToInt(L.X), FMath::FloorToInt(L.Y), FMath::FloorToInt(L.Z));
		}

		int64 NumCells() const
		{
			return bBounded ? int64(Count.X) * Count.Y * Count.Z : 0;
		}
	};

	/** Share of a probe's pixels that see nothing within the far clip: sky. */
	inline float SkyShare(const FProbe& Probe)
	{
		int64 Sky = 0, All = 0;
		for (const TArray<float>& Face : Probe)
		{
			for (float D : Face)
			{
				Sky += D <= 0.f;
				++All;
			}
		}
		return All > 0 ? float(double(Sky) / All) : 0.f;
	}

	/**
	 * Camera positions affordable for training: all pictures stay in memory, so the
	 * budget is in pixels (1.5 gigapixels, about 6 GB of RAM with depth). 390
	 * positions at 800 px, 610 at 640 px.
	 */
	inline int32 AutoMaxPositions(int32 ImageSize)
	{
		return FMath::Max(30, int32(1.5e9 / (6.0 * double(ImageSize) * ImageSize)));
	}

	/**
	 * Distance to the nearest surface for every pixel of a probe face, from depth
	 * along the view axis (cm). Depth beyond `Far` or not positive counts as nothing.
	 */
	inline TArray<float> FaceDistances(const TArray<FLinearColor>& Depth, int32 N, float Far)
	{
		TArray<float> Out;
		Out.SetNumUninitialized(N * N);
		for (int32 Y = 0; Y < N; ++Y)
		{
			const float TY = (Y + 0.5f) / N * 2.f - 1.f;
			for (int32 X = 0; X < N; ++X)
			{
				const float TX = (X + 0.5f) / N * 2.f - 1.f;
				const float D = Depth[Y * N + X].R;
				Out[Y * N + X] = (D > 0.f && D < Far) ? D * FMath::Sqrt(1.f + TX * TX + TY * TY) : 0.f;
			}
		}
		return Out;
	}

	/** Distance from the probe point to the nearest surface in any direction. */
	inline float Clearance(const FProbe& Probe)
	{
		float Nearest = TNumericLimits<float>::Max();
		for (const TArray<float>& Face : Probe)
		{
			for (float D : Face)
			{
				if (D > 0.f && D < Nearest)
				{
					Nearest = D;
				}
			}
		}
		return Nearest;
	}

	/**
	 * True when the point `To` is in view from the probe taken at `From`, with
	 * `Margin` to spare before the surface behind it. `Faces` are the face
	 * rotations, `N` the probe's width in pixels.
	 */
	inline bool Sees(const FProbe& Probe, const TArray<FQuat>& Faces, int32 N, const FVector& From, const FVector& To, float Margin)
	{
		const FVector Offset = To - From;
		const double Dist = Offset.Size();
		if (Dist < 1e-3)
		{
			return true;
		}
		// The face the direction falls on is the one it points along most.
		int32 Face = 0;
		double Forward = -1e30;
		FVector Cam = FVector::ZeroVector;
		for (int32 I = 0; I < 6; ++I)
		{
			const FVector C = Faces[I].UnrotateVector(Offset);
			if (C.X > Forward)
			{
				Forward = C.X;
				Face = I;
				Cam = C;
			}
		}
		const int32 PX = FMath::FloorToInt((Cam.Y / Forward * 0.5 + 0.5) * N);
		const int32 PY = FMath::FloorToInt((-Cam.Z / Forward * 0.5 + 0.5) * N);
		// The neighbouring pixels count too: a thin obstacle may fall between two rays.
		for (int32 DY = -1; DY <= 1; ++DY)
		{
			for (int32 DX = -1; DX <= 1; ++DX)
			{
				const int32 X = FMath::Clamp(PX + DX, 0, N - 1);
				const int32 Y = FMath::Clamp(PY + DY, 0, N - 1);
				const float D = Probe[Face][Y * N + X];
				if (D > 0.f && D < Dist + Margin)
				{
					return false;
				}
			}
		}
		return true;
	}

	/**
	 * At most `Count` of `Points`, spread as evenly as possible. The first `Keep`
	 * points (the start points) always stay. Returns the chosen indices in order.
	 */
	inline TArray<int32> Spread(const TArray<FVector>& Points, int32 Count, int32 Keep)
	{
		TArray<int32> Chosen;
		const int32 Total = Points.Num();
		if (Total <= Count)
		{
			for (int32 I = 0; I < Total; ++I)
			{
				Chosen.Add(I);
			}
			return Chosen;
		}
		// Farthest point first: each new point is the one farthest from all chosen so far.
		TArray<double> Nearest;
		Nearest.Init(1e300, Total);
		int32 Next = 0;
		while (Chosen.Num() < Count)
		{
			Chosen.Add(Next);
			const FVector P = Points[Next];
			double Best = -1.0;
			const int32 Following = Chosen.Num() < Keep ? Chosen.Num() : -1;
			for (int32 I = 0; I < Total; ++I)
			{
				Nearest[I] = FMath::Min(Nearest[I], FVector::DistSquared(P, Points[I]));
				if (Nearest[I] > Best)
				{
					Best = Nearest[I];
					Next = I;
				}
			}
			if (Following >= 0)
			{
				Next = Following;
			}
		}
		Chosen.Sort();
		return Chosen;
	}

	/** Uniformly random rotation (Shoemake). */
	inline FQuat RandomRotation(FRandomStream& Rng)
	{
		const double U1 = Rng.FRand(), U2 = Rng.FRand() * UE_DOUBLE_TWO_PI, U3 = Rng.FRand() * UE_DOUBLE_TWO_PI;
		const double A = FMath::Sqrt(1.0 - U1), B = FMath::Sqrt(U1);
		return FQuat(A * FMath::Sin(U2), A * FMath::Cos(U2), B * FMath::Sin(U3), B * FMath::Cos(U3)).GetNormalized();
	}

	/**
	 * Unreal (cm; X forward, Y right, Z up; left-handed) to Godot (m; X right,
	 * Y up, Z back; right-handed). Unreal's forward becomes Godot's -Z.
	 */
	inline FVector ToGodotDirection(const FVector& V)
	{
		return FVector(V.Y, V.Z, -V.X);
	}

	/**
	 * A transform as Godot writes a Transform3D: 12 numbers, the basis vectors x, y,
	 * z, then the origin, in metres. An Unreal camera (looking along X, Z up) becomes
	 * a Godot camera (looking along -Z, Y up): right, up and back.
	 */
	inline TArray<double> GodotNumbers(const FQuat& Rotation, const FVector& Location)
	{
		const FVector X = ToGodotDirection(Rotation.GetRightVector());
		const FVector Y = ToGodotDirection(Rotation.GetUpVector());
		const FVector Z = -ToGodotDirection(Rotation.GetForwardVector());
		const FVector O = ToGodotDirection(Location) / 100.0;
		return {X.X, X.Y, X.Z, Y.X, Y.Y, Y.Z, Z.X, Z.Y, Z.Z, O.X, O.Y, O.Z};
	}
}
