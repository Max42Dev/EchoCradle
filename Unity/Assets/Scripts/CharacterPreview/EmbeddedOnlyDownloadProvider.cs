using System;
using System.Threading.Tasks;
using GLTFast.Loading;

namespace EchoCradle.CharacterPreview
{
    // GLB buffers/images must be embedded. Never follow file: or network URIs
    // supplied by generated content. glTFast handles embedded data internally.
    internal sealed class EmbeddedOnlyDownloadProvider : IDownloadProvider
    {
        public Task<IDownload> Request(Uri url)
        {
            throw new NotSupportedException("Character previews require self-contained GLB resources.");
        }

        public Task<ITextureDownload> RequestTexture(Uri url, bool nonReadable)
        {
            throw new NotSupportedException("Character previews require embedded GLB textures.");
        }
    }
}