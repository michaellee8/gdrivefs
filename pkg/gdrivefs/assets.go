package gdrivefs

import (
	"bytes"
	"crypto/sha256"
	"github.com/gobuffalo/packr/v2"
	"io"
	"log"
	"os"
)

var assetsBox = packr.New("assets", "../../assets")

const tmpDir = "/tmp/gdrivefs"

func verifyFileCheckSum(path string, originalData []byte) (bool, error) {
	originalHash := sha256.Sum256(originalData)
	f, err := os.Open(path)
	if err != nil {
		return false, err
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return false, err
	}
	fileHash := h.Sum(nil)
	return bytes.Equal(originalHash[:], fileHash), nil
}

func extractAssetsIfRequired() error {
	directoryInfo, err := os.Stat(tmpDir)
	if os.IsNotExist(err) {
		// The temp directory does not exists yet, extract it.
		err := os.MkdirAll(tmpDir, 0755)
		if err != nil {
			return err
		}
	} else if err != nil {
		// Something unexpected happened, return the error.
		return err
	}

}
