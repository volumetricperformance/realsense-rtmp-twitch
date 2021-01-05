#!python3
import numpy as np
import cv2
import os
import json
import time
import sys
import platform
import asyncio
from timeit import default_timer as timer

import multiprocessing.queues as mpq
from multiprocessing import Process, Queue
import multiprocessing as mp

import gi
gi.require_version('Gst', '1.0')
from gi.repository import GObject, Gst

class GStreamerSender(mp.Process):
    def __init__(self, rtmp_url, width, height, statusQueue, messageQueue, previewQueue):
        mp.Process.__init__(self)

        self.rtmp_url = rtmp_url
        self.width = width
        self.height = height
        self.messageQueue = messageQueue
        self.statusQueue = statusQueue
        self.previewQueue = previewQueue
        self.exit = mp.Event()
        print("Initialized GStreamerSender")

    def run(self):
        # ===========================================
        # 6. Setup gstreamer
        # Usefull gst resources / cheatsheets
        # https://github.com/matthew1000/gstreamer-cheat-sheet/blob/master/rtmp.md
        # http://wiki.oz9aec.net/index.php/Gstreamer_cheat_sheet
        # https://github.com/matthew1000/gstreamer-cheat-sheet/blob/master/mixing.md
        # ===========================================     
        CLI = ''
        caps =  'caps="video/x-raw,format=BGR,width='+str(self.width)+',height='+ str(self.height*2) + ',framerate=(fraction)30/1,pixel-aspect-ratio=(fraction)1/1"'
        
        if platform.system() == "Linux":
            #assuming Linux means RPI
            
            CLI='flvmux name=mux streamable=true latency=3000000000 ! rtmpsink location="'+  self.rtmp_url +' live=1 flashver=FME/3.0%20(compatible;%20FMSc%201.0)" \
                appsrc name=mysource format=TIME do-timestamp=TRUE is-live=TRUE '+ str(caps) +' ! \
                videoconvert !  omxh264enc ! h264parse ! video/x-h264 ! \
                queue max-size-buffers=0 max-size-bytes=0 max-size-time=180000000 min-threshold-buffers=1 leaky=upstream ! mux. \
                alsasrc ! audio/x-raw, format=S16LE, rate=44100, channels=1 ! voaacenc bitrate=44100 ! aacparse ! audio/mpeg, mpegversion=4 ! \
                queue max-size-buffers=0 max-size-bytes=0 max-size-time=4000000000 min-threshold-buffers=1 ! mux.'

        elif platform.system() == "Darwin":
            #macos
            #CLI='flvmux name=mux streamable=true ! rtmpsink location="'+  self.rtmp_url +' live=1 flashver=FME/3.0%20(compatible;%20FMSc%201.0)" \
            #    appsrc name=mysource format=TIME do-timestamp=TRUE is-live=TRUE '+ str(caps) +' ! \
            #    videoconvert ! vtenc_h264 ! video/x-h264 ! h264parse ! video/x-h264 ! \
            #    queue max-size-buffers=4 ! flvmux name=mux. \
            #    osxaudiosrc do-timestamp=true ! audioconvert ! audioresample ! audio/x-raw,rate=48000 ! faac bitrate=48000 ! audio/mpeg ! aacparse ! audio/mpeg, mpegversion=4 ! \
            #    queue max-size-buffers=4 ! mux.'

            CLI='appsrc name=mysource format=TIME do-timestamp=TRUE is-live=TRUE caps="video/x-raw,format=BGR,width='+str(self.width)+',height='+ str(self.height*2) + ',framerate=(fraction)30/1,pixel-aspect-ratio=(fraction)1/1" ! videoconvert ! vtenc_h264 ! video/x-h264 ! h264parse ! video/x-h264 ! queue max-size-buffers=4 ! flvmux name=mux ! rtmpsink location="'+ self.rtmp_url +'" sync=true   osxaudiosrc do-timestamp=true ! audioconvert ! audioresample ! audio/x-raw,rate=48000 ! faac bitrate=48000 ! audio/mpeg ! aacparse ! audio/mpeg, mpegversion=4 ! queue max-size-buffers=4 ! mux.' 


        #TODO: windows

        print( CLI )
        self.gstpipe=Gst.parse_launch(CLI)
        #Set up a pipeline bus watch to catch errors.
        self.bus = self.gstpipe.get_bus()
        self.bus.connect("message", self.on_bus_message)
        self.appsrc=self.gstpipe.get_by_name("mysource")
        self.appsrc.set_property('emit-signals',True) #tell sink to emit signals 
        self.gstpipe.set_state(Gst.State.PLAYING)

        print("Starting message loop")
        buff = None
        depth_hsv = np.zeros((480, 640, 3), dtype=np.float32)

        while not self.exit.is_set():
            try:
                while(not self.messageQueue.empty()):
                    start = timer()
                    (color_image,depth_image) = self.messageQueue.get_nowait()
                    #color is nparray of rl color, depth is nparray of rl depth

                    # We need to encode/pack the 16bit depth value to RGB
                    # we do this by treating it as the Hue in HSV. 
                    # we then encode HSV to RGB and stream that
                    # on the other end we reverse RGB to HSV, H will give us the depth value back.
                    # HSV elements are in the 0-1 range so we need to normalize the depth array to 0-1
                    # First set a far plane and set everything beyond that to 0
                    
                    clipped = depth_image > 4000
                    depth_image[clipped] = 0

                    # Now normalize using that far plane
                    # cv expects the H in degrees, not 0-1 :(
                    depth_image *= (360/4000)
                    depth_hsv[:,:,0] = depth_image
                    depth_hsv[:,:,1] = 1
                    depth_hsv[:,:,2] = 1
                    discard = depth_image == 0
                    s = depth_hsv[:,:,1]
                    v = depth_hsv[:,:,2] 
                    s[ discard] = 0
                    v[ discard] = 0
                    
                    # cv2.cvtColor to convert HSV to RGB
                    hsv = cv2.cvtColor(depth_hsv, cv2.COLOR_HSV2BGR_FULL)

                    # cv2 needs hsv to 8bit (0-255) to stack with the color image
                    hsv8 = (hsv*255).astype( np.uint8)
                    
                    # Stack both images horizontally
                    image = np.vstack((color_image, hsv8))       
                    frame = image.tostring()
                    if buff is None:
                        buff = Gst.Buffer.new_allocate(None, len(frame), None)
                    buff.fill(0,frame)
                    self.appsrc.emit("push-buffer", buff)
                    #process any messages from gstreamer
                    msg = self.bus.pop_filtered(
                        Gst.MessageType.ERROR | Gst.MessageType.WARNING | Gst.MessageType.EOS | Gst.MessageType.INFO | Gst.MessageType.STATE_CHANGED
                    )
                    #msgprocesstime = timer()
                    #print(str(msgprocesstime-start) + " gstreamer process time")
                    #empty the message queue if there is one
                    #start = timer()
                    while( msg ): 
                        self.on_bus_message(msg)
                        msg = self.bus.pop_filtered(
                            Gst.MessageType.ERROR | Gst.MessageType.WARNING | Gst.MessageType.EOS | Gst.MessageType.INFO | Gst.MessageType.STATE_CHANGED
                        )

                    if(not self.exit.is_set()):
                        try:
                            if(not self.previewQueue.full()):
                                self.previewQueue.put_nowait((color_image, hsv8))
                        except:
                            pass

                    msgprocesstime = timer()
                    print("gstreamer frame: %s" % str(1/(msgprocesstime-start)))
                    time.sleep(0.005) 

            except queue.Empty:
                pass
        try:
            if( self.gstpipe.get_state()[1] is not Gst.State.PAUSED ):
                self.gstpipe.set_state(Gst.State.PAUSED)
        except:
            self.statusQueue.put_nowait("ERROR: Error pausing gstreamer")
            print ("Error pausing gstreamer")    


    def shutdown(self):
        print("Shutdown GStreamerSender")
        self.exit.set()

    def on_bus_message(self, message):
        t = message.type
        
        print('{} {}: {}'.format(
                Gst.MessageType.get_name(message.type), message.src.name,
                message.get_structure().to_string()))

        if t == Gst.MessageType.EOS:
            print("Eos")
            self.statusQueue.put_nowait('WARNING: End of Stream')

        elif t == Gst.MessageType.INFO:
            self.statusQueue.put_nowait('INFO: %s, %s' % (msg.src.name, msg.get_structure().to_string()))

        elif t == Gst.MessageType.STATE_CHANGED:
            old_state, new_state, pending_state = message.parse_state_changed()
            #print("Pipeline state changed from %s to %s." %  (old_state.value_nick, new_state.value_nick))
            self.statusQueue.put_nowait("STREAM_STATE_CHANGED: %s, %s, %s" % (message.src.name, old_state.value_nick, new_state.value_nick))

        elif t == Gst.MessageType.WARNING:
            err, debug = message.parse_warning()
            print('Warning: %s: %s\n' % (err, debug))
            self.statusQueue.put_nowait('WARNING: %s, %s' % (err, debug) )
            #sys.stderr.write('Warning: %s: %s\n' % (err, debug))
        elif t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            print('Error: %s: %s\n' % (err, debug))
            self.statusQueue.put_nowait('ERROR: %s, %s' % (err, debug) )
            self.shutdown()
            #sys.stderr.write('Error: %s: %s\n' % (err, debug))       
        return True

