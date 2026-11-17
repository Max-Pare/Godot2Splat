// Editor side of the godot2splat capture: a "Scan to Splat" toolbar button and a
// command line entry.
//
// The button scans the open level here in the editor (see CaptureJob.h), then runs
// the g2s command line tool for the rest (training, cleanup, export into the Godot
// project named on the Splat Capture Volume) in a separate process and shows its
// progress. Which command to run is read from <project>/.g2s/command.json, written
// by `g2s install <Unreal project>`.
//
// Command line (used by `g2s build`): starting the editor with
//   -G2SCapture=<output folder> -G2SMap=/Game/Path/Map
// scans that map as soon as it is open, then quits with exit code 0 on success.
// Optional:
//   -G2SOrigin=<actor label>   scan from a temporary volume at that actor instead of
//                              the level's Splat Capture Volume; the level is not changed
//   -G2SBox=auto | LxWxH       auto: fill the open space (Placement Fill); LxWxH: cameras
//                              inside a box of that size in metres along X, Y, Z
//   -G2SSet="Name=Value;..."   volume settings, e.g. "MaxPositions=200;ImageSize=640"

#include "CaptureJob.h"
#include "Components/BoxComponent.h"
#include "Dom/JsonObject.h"
#include "Editor.h"
#include "FileHelpers.h"
#include "Framework/Notifications/NotificationManager.h"
#include "HAL/FileManager.h"
#include "Misc/CommandLine.h"
#include "Misc/FileHelper.h"
#include "Misc/MessageDialog.h"
#include "Misc/MonitoredProcess.h"
#include "Misc/PackageName.h"
#include "Misc/Paths.h"
#include "Modules/ModuleManager.h"
#include "Serialization/JsonReader.h"
#include "Serialization/JsonSerializer.h"
#include "SplatCaptureVolume.h"
#include "Styling/AppStyle.h"
#include "ToolMenus.h"
#include "Widgets/Notifications/SNotificationList.h"

#define LOCTEXT_NAMESPACE "G2SCapture"

class FG2SCaptureEditorModule : public IModuleInterface
{
public:
	virtual void StartupModule() override
	{
		UToolMenus::RegisterStartupCallback(FSimpleMulticastDelegate::FDelegate::CreateRaw(this, &FG2SCaptureEditorModule::RegisterMenus));

		FString OutDir;
		if (FParse::Value(FCommandLine::Get(), TEXT("G2SCapture="), OutDir))
		{
			FParse::Value(FCommandLine::Get(), TEXT("G2SMap="), CommandLineMap);
			CommandLineOut = FPaths::ConvertRelativePathToFull(OutDir);
			StartupHandle = FTSTicker::GetCoreTicker().AddTicker(FTickerDelegate::CreateRaw(this, &FG2SCaptureEditorModule::TickCommandLine));
		}
	}

	virtual void ShutdownModule() override
	{
		if (StartupHandle.IsValid())
		{
			FTSTicker::GetCoreTicker().RemoveTicker(StartupHandle);
		}
		if (ProgressHandle.IsValid())
		{
			FTSTicker::GetCoreTicker().RemoveTicker(ProgressHandle);
		}
		if (Job.IsValid())
		{
			Job->Cancel();
		}
		if (Process.IsValid())
		{
			Process->Cancel(true);
		}
		UToolMenus::UnRegisterStartupCallback(this);
		UToolMenus::UnregisterOwner(this);
	}

private:
	TSharedPtr<FG2SCaptureJob> Job;
	TSharedPtr<FMonitoredProcess> Process;
	TSharedPtr<SNotificationItem> Notification;
	FTSTicker::FDelegateHandle StartupHandle;
	FTSTicker::FDelegateHandle ProgressHandle;
	FString CommandLineOut;
	FString CommandLineMap;
	int32 StartupTicks = 0;
	FCriticalSection LineLock;
	FString LastLine;

	static UWorld* EditorWorld()
	{
		return GEditor != nullptr ? GEditor->GetEditorWorldContext().World() : nullptr;
	}

	// --- command line -------------------------------------------------------

	bool TickCommandLine(float)
	{
		UWorld* World = EditorWorld();
		// Wait until the editor is up with the requested map open and nothing is loading.
		const bool bMapOpen = World != nullptr && (CommandLineMap.IsEmpty() || World->GetOutermost()->GetName() == CommandLineMap);
		if (++StartupTicks < 30 || !bMapOpen || GIsSlowTask || IsAsyncLoading())
		{
			if (StartupTicks > 3000)
			{
				UE_LOG(LogG2S, Error, TEXT("capture: ERROR the map %s did not open"), *CommandLineMap);
				FPlatformMisc::RequestExitWithStatus(false, 1);
				return false;
			}
			return true;
		}
		StartupHandle.Reset();
		auto Quit = [](const FString& Error)
		{
			UE_LOG(LogG2S, Error, TEXT("capture: ERROR %s"), *Error);
			FPlatformMisc::RequestExitWithStatus(false, 1);
			return false;
		};
		FString Error, Origin, BoxArg, Settings;
		FParse::Value(FCommandLine::Get(), TEXT("G2SOrigin="), Origin);
		FParse::Value(FCommandLine::Get(), TEXT("G2SBox="), BoxArg);
		FParse::Value(FCommandLine::Get(), TEXT("G2SSet="), Settings, false);
		ASplatCaptureVolume* Volume = Origin.IsEmpty() ? FG2SCaptureJob::FindVolume(World, Error)
			: FG2SCaptureJob::MakeTemporaryVolume(World, Origin, Error);
		if (Volume == nullptr)
		{
			return Quit(Error);
		}
		if (!Settings.IsEmpty())
		{
			Error = FG2SCaptureJob::ApplySettings(Volume, Settings);
			if (!Error.IsEmpty())
			{
				return Quit(Error);
			}
		}
		Job = MakeShared<FG2SCaptureJob>(World, Volume, CommandLineOut, World->GetOutermost()->GetName());
		Job->SetOwnsVolume(!Origin.IsEmpty());
		if (BoxArg == TEXT("auto"))
		{
			Volume->Placement = ESplatCapturePlacement::Fill;
		}
		else if (!BoxArg.IsEmpty())
		{
			// Size in metres along Unreal's X, Y, Z: "20x6x4".
			TArray<FString> Parts;
			BoxArg.ParseIntoArray(Parts, TEXT("x"));
			if (Parts.Num() != 3)
			{
				return Quit(FString::Printf(TEXT("box size '%s' is not LxWxH in metres, nor auto"), *BoxArg));
			}
			Volume->Placement = ESplatCapturePlacement::Box;
			Volume->Box->SetBoxExtent(FVector(FCString::Atof(*Parts[0]), FCString::Atof(*Parts[1]), FCString::Atof(*Parts[2])) * 50.0, false);
		}
		Job->Start(FG2SCaptureJob::FOnFinished::CreateLambda([](int32 Code)
		{
			FPlatformMisc::RequestExitWithStatus(false, uint8(Code));
		}));
		return false;
	}

	// --- toolbar button -----------------------------------------------------

	void RegisterMenus()
	{
		FToolMenuOwnerScoped Owner(this);
		UToolMenu* Menu = UToolMenus::Get()->ExtendMenu("LevelEditor.LevelEditorToolBar.User");
		FToolMenuSection& Section = Menu->FindOrAddSection("G2SCapture");
		FToolMenuEntry Entry = FToolMenuEntry::InitToolBarButton(
			"ScanToSplat",
			FUIAction(FExecuteAction::CreateRaw(this, &FG2SCaptureEditorModule::OnScan)),
			LOCTEXT("ScanToSplat", "Scan to Splat"),
			LOCTEXT("ScanToSplatTip", "Scan this level into a Gaussian splat with g2s and put it into the Godot project set on the Splat Capture Volume (saves the level first)."),
			FSlateIcon(FAppStyle::GetAppStyleSetName(), "ClassIcon.SceneCapture2D"));
		Section.AddEntry(Entry);
	}

	static void Message(const FString& Text)
	{
		FMessageDialog::Open(EAppMsgType::Ok, FText::FromString(Text), LOCTEXT("Title", "Scan to Splat"));
	}

	void OnScan()
	{
		if ((Job.IsValid() && Job->IsRunning()) || (Process.IsValid() && Process->Update()))
		{
			Message(TEXT("A scan is already running."));
			return;
		}
		UWorld* World = EditorWorld();
		FString Error;
		ASplatCaptureVolume* Volume = FG2SCaptureJob::FindVolume(World, Error);
		if (Volume == nullptr)
		{
			Message(Error);
			return;
		}
		const FString Godot = Volume->GodotProject.Path;
		if (Godot.IsEmpty() || !FPaths::FileExists(FPaths::Combine(Godot, TEXT("project.godot"))))
		{
			Message(TEXT("Set Godot Project on the Splat Capture Volume to the folder of a Godot project (it holds project.godot). The splat is put there."));
			return;
		}
		if (!FEditorFileUtils::SaveCurrentLevel() || FPackageName::IsTempPackage(World->GetOutermost()->GetName()))
		{
			Message(TEXT("Save the level to a file first."));
			return;
		}
		const FString ProjectDir = FPaths::ConvertRelativePathToFull(FPaths::ProjectDir());
		TArray<FString> Command;
		FString Text;
		if (FFileHelper::LoadFileToString(Text, *FPaths::Combine(ProjectDir, TEXT(".g2s/command.json"))))
		{
			TSharedPtr<FJsonObject> Json;
			if (FJsonSerializer::Deserialize(TJsonReaderFactory<>::Create(Text), Json) && Json.IsValid())
			{
				Json->TryGetStringArrayField(TEXT("argv"), Command);
			}
		}
		if (Command.Num() == 0)
		{
			Message(FString::Printf(TEXT("%s.g2s/command.json is missing. Run: g2s install \"%s\""), *ProjectDir, *ProjectDir));
			return;
		}

		const FString MapPath = World->GetOutermost()->GetName();
		const FString Workspace = FPaths::Combine(ProjectDir, TEXT(".g2s"), FPackageName::GetShortName(MapPath));
		ShowNotification(TEXT("Scan to Splat: starting"));
		Job = MakeShared<FG2SCaptureJob>(World, Volume, FPaths::Combine(Workspace, TEXT("capture")), MapPath);
		Job->Start(FG2SCaptureJob::FOnFinished::CreateLambda([this, Command, ProjectDir, MapPath](int32 Code)
		{
			// Let the job finish its own tick before it is released.
			FTSTicker::GetCoreTicker().AddTicker(FTickerDelegate::CreateLambda([this, Code, Command, ProjectDir, MapPath](float)
			{
				Job.Reset();
				if (Code != 0)
				{
					Done(false, TEXT("Scan to Splat: the scan failed; see the Output Log (LogG2S)."));
				}
				else
				{
					RunG2S(Command, ProjectDir, MapPath);
				}
				return false;
			}));
		}));
	}

	void RunG2S(const TArray<FString>& Command, const FString& ProjectDir, const FString& MapPath)
	{
		TArray<FString> Args(Command);
		Args.RemoveAt(0);
		Args.Append({TEXT("build"), ProjectDir, MapPath, TEXT("--captured")});
		FString Params;
		for (const FString& A : Args)
		{
			Params += (Params.IsEmpty() ? TEXT("") : TEXT(" ")) + (A.Contains(TEXT(" ")) ? TEXT("\"") + A + TEXT("\"") : A);
		}
		UE_LOG(LogG2S, Display, TEXT("running: %s %s"), *Command[0], *Params);
		Process = MakeShared<FMonitoredProcess>(Command[0], Params, ProjectDir, true, true);
		Process->OnOutput().BindLambda([this](const FString& Line)
		{
			if (!Line.TrimStartAndEnd().IsEmpty())
			{
				UE_LOG(LogG2S, Display, TEXT("%s"), *Line);
				FScopeLock Lock(&LineLock);
				LastLine = Line;
			}
		});
		Process->OnCompleted().BindLambda([this](int32 Code)
		{
			AsyncTask(ENamedThreads::GameThread, [this, Code]()
			{
				FString Last;
				{
					FScopeLock Lock(&LineLock);
					Last = LastLine;
				}
				Done(Code == 0, Code == 0 ? FString::Printf(TEXT("Scan to Splat: done. %s"), *Last)
					: TEXT("Scan to Splat: g2s failed; see the Output Log (LogG2S)."));
			});
		});
		if (!Process->Launch())
		{
			Done(false, FString::Printf(TEXT("Scan to Splat: cannot start %s. Run g2s install again."), *Command[0]));
		}
	}

	// --- progress -----------------------------------------------------------

	void ShowNotification(const FString& Text)
	{
		FNotificationInfo Info(FText::FromString(Text));
		Info.bFireAndForget = false;
		Info.bUseThrobber = true;
		Info.ExpireDuration = 0.f;
		Info.ButtonDetails.Add(FNotificationButtonInfo(LOCTEXT("Stop", "Stop"), FText(),
			FSimpleDelegate::CreateRaw(this, &FG2SCaptureEditorModule::Stop), SNotificationItem::CS_Pending));
		Notification = FSlateNotificationManager::Get().AddNotification(Info);
		if (Notification.IsValid())
		{
			Notification->SetCompletionState(SNotificationItem::CS_Pending);
		}
		ProgressHandle = FTSTicker::GetCoreTicker().AddTicker(FTickerDelegate::CreateLambda([this](float)
		{
			FString Text;
			if (Job.IsValid())
			{
				Text = TEXT("Scan to Splat: ") + Job->GetProgress().ToString();
			}
			else
			{
				FScopeLock Lock(&LineLock);
				Text = TEXT("Scan to Splat: ") + LastLine;
			}
			if (Notification.IsValid())
			{
				Notification->SetText(FText::FromString(Text.Left(160)));
			}
			return true;
		}), 0.25f);
	}

	void Stop()
	{
		if (Job.IsValid())
		{
			Job->Cancel();
		}
		if (Process.IsValid())
		{
			Process->Cancel(true);
		}
	}

	void Done(bool bOk, const FString& Text)
	{
		UE_LOG(LogG2S, Display, TEXT("%s"), *Text);
		if (ProgressHandle.IsValid())
		{
			FTSTicker::GetCoreTicker().RemoveTicker(ProgressHandle);
			ProgressHandle.Reset();
		}
		if (Notification.IsValid())
		{
			Notification->SetText(FText::FromString(Text.Left(300)));
			Notification->SetCompletionState(bOk ? SNotificationItem::CS_Success : SNotificationItem::CS_Fail);
			Notification->SetExpireDuration(bOk ? 10.f : 20.f);
			Notification->ExpireAndFadeout();
			Notification.Reset();
		}
	}
};

IMPLEMENT_MODULE(FG2SCaptureEditorModule, G2SCaptureEditor)

#undef LOCTEXT_NAMESPACE
