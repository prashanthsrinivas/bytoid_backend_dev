
import boto3
from typing import Optional
import os

class PollyService:
    def __init__(self, aws_access_key_id: str, aws_secret_access_key: str, region_name: str = "us-east-1"):
        """
        Initialize the Polly service with AWS credentials
        
        Args:
            aws_access_key_id: AWS access key ID
            aws_secret_access_key: AWS secret access key
            region_name: AWS region name (default: us-east-1)
        """
        self.polly_client = boto3.client(
            'polly',
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
            region_name=region_name
        )

    def synthesize_speech(
        self,
        text: str,
        output_path: str,
        voice_id: str = "",
        engine: str = "neural",
        language_code: str = "en-US"
    ) -> Optional[str]:
        """
        Convert text to speech using Amazon Polly and save as MP3
        
        Args:
            text: The text to convert to speech
            output_path: Path where the MP3 file should be saved
            voice_id: Polly voice ID (default: Joanna)
            engine: Polly engine type (default: neural)
            language_code: Language code (default: en-US)
            
        Returns:
            The path to the saved MP3 file if successful, None otherwise
        """
        try:
            # Request speech synthesis
            response = self.polly_client.synthesize_speech(
                Engine=engine,
                LanguageCode=language_code,
                Text=text,
                OutputFormat='mp3',
                VoiceId=voice_id
            )

            # Save the audio stream to file
            if "AudioStream" in response:
                with open(output_path, 'wb') as audio_file:
                    audio_file.write(response['AudioStream'].read())
                return output_path
            return None

        except Exception as e:
            print(f"Error synthesizing speech: {str(e)}")
            return None

    def list_voices(self, language_code: Optional[str] = None) -> list:
        """
        List available voices for the specified language
        
        Args:
            language_code: Optional language code to filter voices
            
        Returns:
            List of available voices
        """
        try:
            if language_code:
                response = self.polly_client.describe_voices(LanguageCode=language_code)
            else:
                response = self.polly_client.describe_voices()
            return response.get('Voices', [])
        except Exception as e:
            print(f"Error listing voices: {str(e)}")
            return []

# Example usage:
if __name__ == "__main__":
    # Initialize the service with your AWS credentials
    polly = PollyService(
        aws_access_key_id="your_access_key",
        aws_secret_access_key="your_secret_key"
    )
    
    # Convert text to speech
    output_file = polly.synthesize_speech(
        text="Hello, this is a test of Amazon Polly text to speech conversion.",
        output_path="test_output.mp3"
    )
    
    if output_file:
        print(f"Audio file saved to: {output_file}")
    else:
        print("Failed to generate audio file")


