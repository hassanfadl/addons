from gui import *
class ErrorDialog:
    def __init__(self,sErrorMsg, sErrorHelpMsg=""):
        self.win = DBModalDialog(50, 50, 150, 70, "Error Message")
        self.win.addFixedText("lblErrMsg", 5, 5, 190, 25, sErrorMsg)
        self.win.addFixedText("lblErrHelpMsg", 5, 30, 190, 25, sErrorHelpMsg)
        self.win.addButton('btnOK', 55,-5,40,15,'Ok'
                     ,actionListenerProc = self.btnOkOrCancel_clicked )
        self.win.doModalDialog()
    def btnOkOrCancel_clicked( self, oActionEvent ):
        self.win.endExecute()