#pragma once
#include "CoreMinimal.h"

class FJsonObject;
class IWebSocket;
class SEditableTextBox;
class SMultiLineEditableTextBox;
class STextBlock;
class SWidget;
class UFreyaAudioProbeComponent;

/**
 * The window's connection to Freya, and the chat drawn over the scene.
 *
 * Freya's Python backend does all the thinking. This sends what the owner
 * types, shows what is said, and hands her speech to the face and the
 * speakers. It holds none of her data and can ask for nothing beyond what
 * her gateway lets a GUI ask for.
 *
 * Everything here runs on the game thread; the messages are small.
 */
class FFreyaLink : public TSharedFromThis<FFreyaLink>
{
public:
    FFreyaLink(UFreyaAudioProbeComponent* InOwner, int32 InPort, const FString& InKey);
    ~FFreyaLink();

    void Start();
    void Stop();
    /** Called every frame by the owning component. */
    void Tick();

private:
    void Connect();
    void HandleText(const FString& Message);
    void HandleBinary(const void* Data, SIZE_T Size, bool bIsLastFragment);
    void HandleEvent(const TSharedPtr<FJsonObject>& Event);
    void SendJson(const TSharedRef<FJsonObject>& Object);
    void Say(const FString& Text);

    void BuildWidget();
    void AddLine(const FString& Who, const FString& Text);
    void RefreshTranscript();
    void SetStatus(const FString& Text);

    TWeakObjectPtr<UFreyaAudioProbeComponent> Owner;
    int32 Port = 0;
    /** The single-use key Freya gave this program, then the session it became. */
    FString Key;
    FString Session;

    TSharedPtr<IWebSocket> Socket;
    TSharedPtr<SWidget> Root;
    TSharedPtr<SMultiLineEditableTextBox> Transcript;
    TSharedPtr<SEditableTextBox> Input;
    TSharedPtr<STextBlock> Status;

    TArray<FString> Lines;
    FString Reply;
    bool bReplying = false;

    /** The utterance being played, and whether Freya has been told it finished. */
    uint32 Utterance = 0;
    bool bUtteranceEnded = false;
    bool bUtteranceOpen = false;
    TArray<uint8> Partial;

    double NextConnectAt = 0.0;
    int32 Attempts = 0;
    bool bStopped = false;
    bool bWelcomed = false;
};
